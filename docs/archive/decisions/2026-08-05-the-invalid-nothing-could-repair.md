# The INVALID nothing could repair, and the count that could hide it

**2026-08-05** · mechanism: `index/store.py` `orphan_vector_ids`/`prune_orphan_vectors`,
`index/validate.py` `vector_row_health`, `cli.py` `clean-orphans` · guarded by BQ8

Carried forward from `2026-08-05-the-name-guard-would-have-published-the-list.md`, which recorded
this as a gap rather than closing it: four fleet stores held 3–8 `vec_chunks` rows whose `chunks`
rows were gone, and they had to be removed with a hand-written `DELETE`. BQ7 fixed the write path
that creates them, but it heals a stranded row only on **re-insert of the same `chunk_id`**, and a
chunk whose source is gone never comes back. `delete_by_path` enumerates from `chunks`, so a row
`chunks` has forgotten is invisible to it; `clear()` is a full wipe. There was no third option —
the one state the validator calls INVALID was the one state the engine could not repair.

`VectorStore.prune_orphan_vectors()` is that third option, and `orphan_vector_ids()` is what names
the rows first. Both read the set difference in SQL (`vec_chunks LEFT JOIN chunks`), so the repair
acts on the same evidence the report shows.

## The count was a proxy that cancels

`orphan_count` was `abs(chunk_count - vec_count)`. Two totals, one subtraction — so a store with one
stranded vector *and* one chunk that never got embedded reads as **zero orphans**, on the check
whose verdict is INVALID. It also cannot name a row, which is why nothing downstream could ever have
acted on it.

`vector_row_health` counts two disjoint sets instead: `stranded_vectors` and `missing_vectors`, with
`orphan_count` their sum. The key keeps its name and its meaning; it is now a sum rather than a
difference. Both new keys are on the `overview(what="validate")` payload because the two faults have
different repairs — a stranded vector is removable, a missing one is only fixed by re-indexing the
file.

BQ8 manufactures both faults together, deliberately: either one alone also fails the old
arithmetic, and only the pair demonstrates the cancellation.

## Found on the machine that was about to be declared clean

The dry run reported one stranded vector in **this repository's own store**, which had been
reporting INVALID. `clean-orphans --yes` took it; the verdict went INVALID → VALID with
`chunk_count` unchanged at 2801. That is the whole reason the repair is a command rather than a note
in a doc: the state is not rare, and the previous answer to it was a manual `DELETE` against a
production store.

## The repair for one orphan nearly killed the repair for the other

The new phase runs before the dir sweep, because that sweep can refuse and exit and a refusal about
whole stores is no reason to withhold the row repair. Then CO1/CO2/CO8 went red: their fixture
writes a `vectors.db` that is not a database, and opening it raised `DatabaseError` out of the
command — aborting before it reached the dir sweep that exists to remove exactly such a store. An
unreadable store is now one line on stderr and the loop continues. Those three tests are the guard:
they fail without it.

Two smaller things the phase has to respect, both already paid for elsewhere in this tree:
**opening a store creates it** (`2026-08-05-a-deleted-root-is-not-a-reindex.md`), so a registered
path with no `vectors.db` is skipped rather than opened; and the handle is `migrate=False`, since a
repair pass has no business rebuilding an FTS index.

## Verification

`38 passed` on `test_binary_quantization.py` + `test_index_validity.py`; the full fast suite
**891 passed, 0 failed**. The end-to-end evidence is the verdict moving on a real store, not the
absence of errors.
