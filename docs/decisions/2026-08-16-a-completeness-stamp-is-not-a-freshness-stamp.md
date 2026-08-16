# A completeness stamp is not a freshness stamp

**2026-08-16** · VF1-VF4, GH9, GH10, SD8 · mechanism: `server/_overview.py` `_overview_status`,
`daemon/sweeps.py` `_vectors_hash_drift` / `_purge_paths`

A session read `overview(what="status")` on a large federation root, saw `indexed_at` from 17 days
earlier and `index_state: degraded`, concluded the index was untrustworthy, and spent the rest of
its work in grep. The index was current to that same hour, and its own store was `ready` with
205,650 symbols. Everything the reader needed to know that was in the payload; nothing in the
payload said it.

Three defects, in ascending order of how much they cost.

## 1. The first field of the payload is not the one the reader wants

`indexed_at` is written by exactly one place — the tail of a full `_index_project`. Incremental
passes advance `last_change_seen` and deliberately leave it alone, and `_reindex_vectors` says so
in a comment. It is a *completeness* stamp: "this store has had a full graph+vector build, as of".
It is not index age, and `now - indexed_at` is not a number that means anything. On the fleet where
this was found, 149 of 237 rows carried the same value from one bulk registration run.

The honest number already existed and was already persisted: `_vectors_baseline` reads the store's
own `meta.source_mtime`, which is the newest source mtime the vectors were built from. The status
payload simply never showed it. It does now, as `vectors_current_through`, and that one field is
what would have ended this before it started.

`indexed_at` keeps its name. `_needs_index` and `validate.py` both read it, and a rename with no
dual-accept has no safe ordering — so the fix is to publish the field that answers the question,
not to relabel the one that doesn't.

## 2. A root reported its worst member's health as its own

Top-level `index_state` is the min-rank over every store in the fan-out, and `symbol_hollow` and
`hierarchy_quality.degenerate` are `any(...)`. A root is itself one member of its own fan-out, so a
root federating a few honest stubs — a 16-symbol store, a 32-symbol store — reads `degraded` and
`degenerate: true` at the top while its own row, further down the same JSON, reads `ready` and not
degenerate. The payload contradicted itself and the reader stopped at the headline, which is what a
headline is for.

Both readings are wanted. "Is anything under this root unhealthy" is a real question, and so is
"is the project I asked about usable" — they are just not the same question, and one field cannot
be both. The rollups keep their meaning (HR20, GH2, GH3, GH7a, GH7b all define it, and flipping the
subject of a guarded invariant is a change to ratify on its own), and the root's own row is now
published beside them as `own_index_state` / `own_symbol_hollow` / `own_hierarchy_quality`, with
`federation_worst_state` naming the aggregate for what it is. GH10 pins the `own_*` fields to the
root's row in `members` rather than deriving them a second time, because two computations of one
fact drift.

## 3. The instinct was right, for a reason the session did not have

The distrust was misplaced and the mechanism behind it is real. Every staleness trigger reduces to
`mtime > watermark`. A history rewrite, a bulk checkout, a restore from archive — each sets mtimes
to the operation's time, and once any later file pushes the watermark past the rewritten ones they
are invisible to every trigger, permanently. Set drift (SD1-SD4) does not see them either: the paths
still exist and are still indexed, so the *set* is correct and only the *content* is wrong.

An audit during the investigation put 8 of that store's 10,097 files in this state. **It did not
survive re-measurement**: re-hashed in full through the production `_content_hash` and the same
`read_text(errors="replace")` the indexer uses, the store came back at **zero drift**, with
`indexed_at` unmoved and the named files still below the watermark — so they had not been repaired
in the interim, and the original count was an artefact of the ad-hoc audit's own hashing rather than
a live instance. The retraction is recorded rather than quietly dropped, because the number is what
the case for this pass was first argued on.

What survives is the mechanism, which is structural and does not need a sighting: a rewrite
falsifies mtime, and no clock-based check can then reach the file. VF2 constructs the state
deterministically and asserts both halves — `_vectors_content_stale` returns nothing for the file
and the hash pass returns it — so the guard does not depend on the fleet happening to hold an
instance. The zero is now the baseline this is expected to hold.

`_vectors_hash_drift` closes it by asking the only question mtime cannot answer: re-hash the file
with the indexer's own `_content_hash` and compare against `file_hashes`. Two properties make it
affordable to run on every pass:

- **Bounded** — 500 rows per project per pass, sized like `_DRIFT_REPAIR_MAX`. A read and a SHA are
  cheap and GPU-free; only the mismatches reach the embedder.
- **Rotating** — the cursor lives in the store's own meta beside `source_mtime`, and the window
  wraps rather than stopping at the end. A store carries its own progress, no new file is
  introduced, and there is no resting point at which the tail of a store is checked less often than
  its head.

VF2 asserts both halves: the mtime trigger returns nothing for the rewritten file *and* the hash
pass returns it. Without the negative arm this would be a second spelling of an existing check
rather than a new one.

## The one found on the way

The orphan purge was vector-only. The graph lane is woken by a *code* fingerprint (HR38), so a
deleted document's symbols wait for an unrelated source file to change before they go — and until
then `graph` and `overview(what="communities")` answer from a file that no longer exists. The
sighting that started this (a document deleted at HEAD still holding 6 symbols) had converged by
the time it was re-checked, which is the expected end state and says nothing about the window
before it; SD8 constructs the window instead, and was red on the unmodified purge.

The tempting fix is to widen the fingerprint to every language the extractor emits symbols for, and
it is the wrong one: HR38 exists so that prose churn does not wake a lane that re-derives the whole
graph, and that reasoning is untouched. The purge is not a trigger. It runs when the vector side has
*already* accepted the deletion, so extending it to `delete_file_symbols` costs nothing and changes
no lane's subject. SD8.

What remains open, deliberately: an *edited* document's symbols still go stale until a code file
changes. Waking a full graph re-derive for a typo in a README is exactly what HR38 refuses, and
nobody has yet measured what the stale positions cost. The deletion case was the one with evidence.

## The rule

**A stamp answers the question it was written for, and a reader will ask it a different one.**
`indexed_at` was correct, precise, and load-bearing for two callers, and it still misled every
reader of the status payload — because it was the only date on offer and it sat first. The fix for
a field being read as something it is not is rarely to change the field; it is to publish the one
that was actually being asked for, next to it.
