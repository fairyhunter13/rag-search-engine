# The repair inherited the blind spot it was fixing

**2026-08-05** · mechanism: `index/store.py`, `index/validate.py`, `cli.py` · guards: BQ8, BQ9

Audit of the same day's `prune_orphan_vectors` work (see
`2026-08-05-the-invalid-nothing-could-repair.md`). It found two things, and the first one is about
the fix itself.

## 1. The bit lane held 25 codes whose vector was gone

A fleet scan of all 153 registered stores found **0 stranded vectors and 25 stranded bit-lane
codes**, on exactly the four stores that had been hand-repaired the day before, and on no others:
8 + 8 + 6 + 3.

The hand-written `DELETE FROM vec_chunks` reached the float32 table and not `vec_chunks_bin` —
the author had one table in mind and the store has two. That is forgivable in a one-off. What is
not is that **the supported repair written to replace it had the same shape**: it gated the
companion delete on `self._bin_ready`, which is False on precisely the stores that owe a backfill,
and a repair pass opens `migrate=False`. The condition excused the delete on the stores most likely
to need it. The gate is now `_has_bin_table()` — the table's existence, which is what the `DELETE`
actually requires — and `orphan_code_ids` / `prune_orphan_codes` sweep the residue a *foreign*
delete leaves. All 25 were pruned via `clean-orphans --yes`; the re-scan is clean.

`validate.py` reports `stranded_codes` beside `orphan_count` rather than inside it, because the
repair is separate: a stranded vector and a stranded code are removed by different statements.
It is in `_is_member_valid`'s zero-check, so the state is INVALID rather than merely visible.

## 2. A stranded code could take a whole query down

`_search_two_stage` shortlists on `vec_chunks_bin`, filters through `chunks`, then looks each
survivor up in `vec_chunks` with `.fetchone()[0]`. A code whose vector is gone survives both
earlier stages — `chunks` still has the row — and then subscripts `None`. **`TypeError`, and it
takes the entire query, not the one chunk.**

This is the only way this store fails loudly, and it is reachable from the exact state section 1
describes: 25 codes across four production stores were one two-stage query away from it for a day.
Now `if blob is None: continue`.

BQ9 is the negative control and was verified as one: against the unfixed `_search_two_stage` it
fails, with the fix it passes. It asserts the shortlist really proposes the orphaned id first
(otherwise the test proves nothing), forces the two-stage lane with a second handle at
`min_two_stage=1`, then asserts the query returns hits without that id.

## Note

`clean-orphans` now runs the row sweep before the dir sweep, unconditionally, and skips a
registered path with no `vectors.db` — opening a store creates it. It also catches a failed open
and continues with one stderr line: the first cut of this raised on the dummy `vectors.db` that
CO1/CO2/CO8 write, aborting the command before it reached the dir sweep that exists to remove
exactly that store. The repair for one kind of orphan killed the repair for the other; those three
tests are the guard.
