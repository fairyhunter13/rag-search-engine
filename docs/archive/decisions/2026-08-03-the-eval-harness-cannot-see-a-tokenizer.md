# The eval harness puts the identifier in the query, so it cannot see a tokenizer change

**2026-08-03** · P5 · `index/store.py` (`FTS_REV`, `identifier_tokens`, `fts_query`, `_open`,
`insert`, `delete_by_path`), `src/tests/live/test_hybrid_retrieval.py`

The FTS5 index was built with the default `unicode61` tokenizer, which splits on non-alphanumerics.
snake_case therefore works — `_content_hash` is `content` + `hash` — and **camelCase does not**:
`getUserName` has no non-alphanumeric in it, so it is one opaque token and the words inside it are
not searchable. Measured over all 377,590 fleet symbol names, the share that is camelCase with no
underscore is **ts 91.2% · java 80.3% · go 75.8% · js 66.9% · php 53.6% · python 8.3%**, fleet total
**29.7%**. Python — the language this engine benchmarks itself on — is the one language where the
tokenizer already worked.

The fix is index-side, because no query-side rewrite can reach a token that was never emitted.
`chunks_fts` gained a second column, `tokens`, holding `identifier_tokens(content)`: the camelCase
sub-tokens of every word that actually splits. `fts_query` now ORs the split phrase alongside the
literal one, so `getUserName`, `get user name` and `getuser name` all reach the same chunk.
`FTS_REV` moved to `fts5-unicode61-idsplit-1` — a re-tokenise, never a re-embed.

## The trap: the A/B said nothing, and it was right to

`scripts/eval_retrieval.py` builds every query as **`qualified_name` plus the definition's own first
line**. The identifier is therefore in the query *verbatim*, and the whole-identifier phrase already
matched under the old tokenizer. Run as the plan asked — `--lane hybrid`, 300 queries, on the
php/js/vue corpus rather than on this repo:

| | recall@1 | MRR | recall@10 |
|---|---:|---:|---:|
| A (baseline) | 0.5200 | — | — |
| B (identifier-aware) | **0.5267** | 0.6355 | 0.8700 |

Paired McNemar over the 268 shared queries: **2-0 for B, p = 0.5**. That is a null, and reading it
as "the change does nothing" is the mistake this record exists to prevent. **The harness cannot ask
the question the change answers.** Its queries never say "user name" for `getUserName`; they say
`getUserName`.

*(Aside worth knowing before the next A/B: the two arms shared only 268 of 300 queries.
`build_query_set` reads the **live** fleet `graph.db` — `project_graph_db`, not the arm's scratch
store — so a watcher-driven re-derive between the two runs moves the question set. Pin the arms in
one sitting, or expect the pairing to shrink.)*

## What was measured instead

`probe_wordform.py` (scratchpad, re-runnable) asks the lexical lane the query the change is for: for
every camelCase-only definition, one query per file, the query is the sub-tokens **as words**
(`get user name`), the gold is the defining file. Same construction for both arms; arm A run with
the pre-change code against a pre-change store.

| | recall@1 | recall@10 |
|---|---:|---:|
| A (baseline) | 0.110 | 0.275 |
| B (identifier-aware) | **0.200** | **0.565** |

Paired McNemar over the 324 shared queries: **recall@1 45-9 for B, p = 7.3e-07**;
**recall@10 111-2 for B, p = 1.2e-30**.

| language | n | A@1 | B@1 | A@10 | B@10 |
|---|---:|---:|---:|---:|---:|
| php | 159 | 0.201 | **0.327** | 0.396 | **0.792** |
| javascript | 140 | 0.071 | **0.179** | 0.279 | **0.593** |
| python | 23 | 0.043 | 0.043 | 0.130 | 0.174 |

**Python does not move, and that is the confirmation rather than a disappointment** — 8.3% of its
names are camel-only, so there was nothing there to recover. The corpus split is the finding: the
two languages carrying the fleet's mass are the two that gain.

This is an **upper bound on the lexical lane**, stated plainly because the number is large: real
queries are rarely the exact sub-token sequence, and the lane is one half of a hybrid that a
reranker then re-orders. It is not an end-to-end estimate. What it establishes is that the
capability was absent and is now present, which is precisely what the hybrid A/B could not see.

## Two design points that are not obvious from the diff

**`tokens` is a real column, not a derived one.** FTS5's `delete` command needs the exact text that
*was* indexed, for **every** column. Recomputing the sub-tokens at delete time would mean that any
future change to the splitter silently corrupts every store built before it, instead of merely
re-tokenising them. Storing them costs **+4.9% on disk** (measured: 122 MB → 128 MB on a
21,194-chunk store, 74% of which carry sub-tokens) and makes the delete path exact by construction.

**A store carrying the old index is dropped and recreated, not migrated.** An fts5 table's column
set is fixed at creation. That is free here: the `FTS_REV` bump already re-tokenises the whole store
via `'rebuild'`. Measured cost on the same 21,194-chunk store: **2.9 s**, `integrity-check` clean.
A read-only handle (`migrate=False`) against an unmigrated store logs the existing "lexical lane
disabled" line and returns `[]` — it degrades, it does not crash.

## The schema change turned a latent race into a crash, and the fix predates it

Adding a column made `_open`'s migration read *and write* schema, and the live suite immediately
surfaced `sqlite3.OperationalError: duplicate column name: tokens` — twice, from
`sweeps.reconcile_projects` → `_vectors_stale` → `VectorStore(vdb)`, **in a daemon thread, next to
867 passing tests**. Two write handles on one store is the daemon's normal state: reconcile's
staleness check opens one while an indexing run holds another.

The race is older than the column — both `fts_rev` and `bin_rev` were already check-then-act, and
two handles could both decide to `rebuild` — it just had no step that *failed* when repeated. So the
fix is not "make the ALTER idempotent"; it is `BEGIN IMMEDIATE` before the `_rev` reads, which makes
the loser block and then re-read the winner's stamp and owe nothing. `PRAGMA busy_timeout=120000`
goes with it, because the blocked handle is waiting on a full backfill (12.6 s on the largest fleet
store) and Python's 5 s default would replace `duplicate column name` with `database is locked`.

`LX5` is the gate, and it fails on the pre-fix code: two threads opening the same pre-revision store
with `migrate=True`, both required to come back `lexical_ready`. It needs `_pre_idsplit_shape` rather
than the existing `_unmigrate` helper, which clears the stamp but leaves the current schema — and the
schema is the whole of what the race is about.

## The gate that broke, and why its assertion was wrong before this change

`test_lx4_scope_filter_never_becomes_an_fts_rowid_constraint` guards against the shape where fts5
serves the rowid set itself and the query re-runs per candidate. Its vacuity check asserted the
control plan ended in `VIRTUAL TABLE INDEX 0:=M1` — and `M1` encodes the **column count**, so the
second column turned the gate red and named the wrong cause. The discriminator is the `=`, which is
fts5 announcing it will serve the rowids; the count is incidental. The assertion now matches
`VIRTUAL TABLE INDEX 0:=`, so the next column to join the lexical index does not re-break it.
