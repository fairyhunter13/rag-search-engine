---
type: Constraint
resource: src/coderag/registry.py, src/coderag/index.py, src/coderag/federation.py
title: Joining a root is a configuration change to an existing project, and it has to reach the index
description: One registry row per resolved path records why it is there; a root that claims a project widens or narrows its effective excludes, and the config signature turns the next pass into a reconcile rather than a no-op.
tags: [federation, registry, reconcile, lifecycle]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T10:05:00Z }
---

# Constraint

Index a repo directly, then have a federation root symlink it. This is the common case, not the
edge case, and it is the one that spans four modules — which is why it is written here rather than
in any one of their docstrings.

The registry holds **one row per resolved path**, and the row records *why* it exists:

```json
{"path": "/…/member-7", "direct": true, "roots": ["/…/big-root"], "enabled": true}
```

`direct` means someone flagged the project itself; `roots` lists every root that claims it.
Discovery **appends** to `roots`. It never creates a second row and never re-indexes from scratch.

# The change has to reach the index, or the answer is stale

Effective config is the union across the member's own file and every root in `roots`, so a new root
usually *narrows* what the member indexes. `store.meta.config_signature` hashes that effective set;
every index pass compares it, and a mismatch makes the pass a **reconcile** rather than a no-op —
delete the chunks for files the new config excludes, add the files it now includes. It rides the
same content-hash diff, so the cost is one walk.

Without this, a project that joins a root excluding 70.9% of its content keeps serving what it was
indexed with, and nothing anywhere notices.

**It hashes the parsed values, not the file.** That is what made the config format switchable: the
per-project file moved from `.coderag.toml` to `.coderag.yaml` and every signature in the fleet came
out byte-identical — `633d0eb003bb8a5b` for the documented shape under both parsers — so not one of
the 256 rows reconciled. A change that starts folding anything syntactic into the digest reindexes
the whole fleet on a whitespace edit; `test_the_signature_did_not_move_when_the_format_did` pins the
digest so that lands as a failure rather than as an overnight rebuild.

It runs in **both directions**. Removing or unflagging a root drops it from each member's `roots`,
which widens the effective config, and the same reconcile re-adds what the root was suppressing. A
member survives its root's removal if `direct` is set or another root still claims it; otherwise the
row goes. **Neither path deletes an index directory** — see
[two tools](../decisions/two-tools-and-the-operator-surface-is-the-cli.md) for why that is absolute.

A test that flags the root *first* never exercises any of this. The sequence that does: index the
member standalone, confirm a hit on a path the root excludes, flag the root, require the hit to
disappear without a rebuild, unflag, require it back.

# Because a root can quietly shrink a project, it is reported rather than inferred

`index` returns each project's `roots` and the count of files its inherited excludes are
suppressing. Someone who indexed a repo directly and then watched half of it become unfindable
should be able to see why in one call.

# The inverse hazard, and the rule that bounds it

The union is worth having: measured before it was applied, one root's excludes matched **917,890 of
1,295,061 chunks — 70.9%, 2.82 GB of float32 across 48,423 files** — and **98.6% of that mass was
byte-identical to a copy already indexed in a sibling member** (one vendored editor bundle appeared
in 18 members). Without the union the fleet index is roughly 3× larger and mostly duplicate.

The inverse risk is equally real: a pattern that matches first-party source makes it silently
unfindable. So the rule is **measure per-pattern single-copy share before extending an exclude
list**. One drafted pattern was dropped on exactly this — it measured 69% single-copy, meaning it
would have deleted first-party code from the index to save almost nothing.
