# A path is a field, not a line of body text — and only the third probe could see it

**2026-08-03** · P5, P10 · `index/store.py` (`FTS_REV`, `_PATH_WEIGHT`, `_open`, `insert`,
`delete_by_path`, `search_lexical`), `src/tests/live/test_hybrid_retrieval.py`

`chunker.chunk_file` prepends `header = f"# {rel}\n"` to every chunk, so the repo-relative path was
**already in the lexical index** — as one line of body text among a few hundred. Zoekt multiplies
symbol and filename term frequencies by 5 for *"roughly 20% improvement across all key metrics"* and
reports the result insensitive to the exact constant. `chunks_fts` therefore gained a third column,
`path`, and `search_lexical` scores with `bm25(chunks_fts, 1.0, 1.0, 5.0)`. `FTS_REV` moved to
`fts5-unicode61-idsplit-pathf-1` — a re-tokenise, never a re-embed; measured 3.6 s on a
21,194-chunk store, `integrity-check` clean.

**Only the filename half of BM25F is reachable today.** A symbol field needs a per-chunk enclosing
symbol, which the extractor does not record (`_structure_or_generic_walk` sets
`qualified_name = s.name`). That is the extraction-track item, not this one.

## Three instruments, and the two that read null were right to

Measured on the php/js/vue corpus, arm B = the identifier-split index without a path field, arm C =
the same store with it. Every number is a paired McNemar over shared queries, never a comparison of
means.

| instrument | what its query looks like | result |
|---|---|---|
| `eval_retrieval.py --lane hybrid`, 300 q | `qualified_name` + the definition's first line | **1-0 for B, p = 1.0** (recall@1 0.5267 both arms) |
| `probe_wordform.py`, 400 q | identifier sub-tokens as words (`get user name`) | @1 **5-2 for B**, p = 0.45; @10 **6-2 for B**, p = 0.29 |
| `probe_pathq.py`, 400 q | the file's basename as words | @1 **77-7 for C, p = 5.1e-16**; @10 **27-4 for C, p = 3.4e-05** |

Arm-level on the third: recall@1 **0.3950 → 0.5875**, recall@10 **0.7625 → 0.8425**, over 348 shared
queries. Per language at @1: python 0.143 → 0.254, javascript 0.312 → 0.831, css 0.154 → 0.538,
json 0.125 → 0.750, markdown 0.667 → 0.933 — **php is the one language that does not move**
(0.649 → 0.597), because CodeIgniter's PHP filenames are already the class name the body repeats.

This is the same lesson as
[the identifier-tokenizer record](2026-08-03-the-eval-harness-cannot-see-a-tokenizer.md), and it is
the reason that record exists: **a null from an instrument that cannot ask the question is not
evidence of no effect.** The first two probes query with an identifier that is in the body; neither
asks for "the file whose *name* is about X". The rule this leaves behind is that a retrieval change
owes a probe whose queries are the shape the change is for, built *before* the arms are read.

**It is kept on a strict reading of P10, not a generous one.** A change with a null on the general
harness would normally be reverted. What justifies keeping this one is that the significant win and
the null are on *disjoint query populations* — the third probe's population is not a subset of the
first's — and the general harness shows no significant harm in either direction (1-0, p = 1.0).
Had the hybrid harness moved *against* it at any significance, the path field would have gone.

Also worth stating: the win is an **upper bound on the lexical lane**. The path-shaped query is the
most favourable shape for the change, the lane is one half of a hybrid, and a cross-encoder re-orders
what fusion produces. What is established is that the capability was absent and is now present.

## Why the whole store is dropped and rebuilt again

An fts5 table's column set is fixed at creation, so adding `path` is a DROP + recreate — the same
mechanic the `tokens` column needed. It is free under the `FTS_REV` bump, which re-tokenises via
`'rebuild'` regardless. Note the cost is paid **once for both**: a store still on
`fts5-unicode61-1` migrates straight to `…-pathf-1` in one pass.

`delete_by_path` and `insert` carry a **fourth** value now, for the same reason they carry a third:
fts5's `delete` command needs the exact text that was indexed **for every column**. The value comes
from `SELECT … path FROM chunks`, not from the `path` argument, so a row whose stored path ever
diverged from its key deletes correctly rather than silently orphaning an index row.

## The gate, and what makes it discriminate

`test_lx6_a_path_term_outranks_the_same_term_in_a_body` indexes two chunks: one whose *path* is
`app/services/invoice/reconciliation_ledger.py` with an unrelated body, and one at `app/notes.py`
whose body says "reconciliation ledger" five times. A gate that merely asserted the file is *found*
would pass with no weight at all — the terms are in the index either way. So the assertion is on
**rank**, and it is verified red: with `_PATH_WEIGHT = 1.0` the notes file wins.

## The comment that named a column count

`search_lexical`'s scope-filter comment cited the plan strings `0:M1` / `0:=M1`. `M1` is the column
count and the index now has three, so the comment was already stale from the `tokens` column and
would go stale again. It now reads `0:M<n>` / `0:=M<n>` with the `=` called out as the whole of the
finding — the same correction `_ROWID_SERVED` got in LX4, for the same reason.
