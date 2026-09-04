---
type: Defect
resource: src/coderag/store.py, tests/test_doctor.py, tests/test_index.py
title: Half of doctor's only structural check compared a table with itself and could only answer 0
description: "`chunks_fts` is an FTS5 table with content='chunks', so a SELECT against it reads `chunks`. The check for index rows with no chunk behind them therefore compared `chunks` with itself, and `orphans()['fts']` was 0 by construction on every store forever. Reproduced: delete one `chunks` row and the count stays 0 while a MATCH returns the dead rowid -- the exact symptom the docstring names. The vec half was correct throughout. Census of the live fleet: 476 stores read, 0 real orphans, so the defect is latent. The fix reads `chunks_fts_docsize`, the index's own row set, and the new arm fails the old code 0 against 1."
tags: [fts5, sqlite, doctor, guards, census]
status: stable
generated: { by: claude/opus-5, at: 2026-09-04T14:00:00Z }
sources:
  - id: orphans
    resource: src/coderag/store.py
  - id: negative-arm
    resource: tests/test_doctor.py
---

# What happened

`store.orphans` is the one structural check `doctor` inherited from the deleted engine, and the
CLI help names it: it catches a delete path that forgot a table, whose symptom is a plausible
search result pointing at a line range that no longer exists.

It ran as two counts side by side:

```sql
SELECT COUNT(*) FROM chunks_fts WHERE rowid NOT IN (SELECT id FROM chunks)   -- always 0
SELECT COUNT(*) FROM chunks_vec WHERE chunk_id NOT IN (SELECT id FROM chunks) -- correct
```

The second one works. The first one cannot.

# Why the first one cannot

`chunks_fts` is declared `content='chunks', content_rowid='id'`. An external-content FTS5 table
holds no copy of the rows. A `SELECT` against it is answered **out of `chunks`**, so the query
above reads `chunks` and asks which of its ids are not in `chunks`. The answer to that is 0 on
every store, in every state, forever.

The module docstring already said `chunks_fts` is external-content. The fact was known and the
consequence was missed, which is the shape worth recording: a true sentence three lines above the
query did not stop the query being written.

# Reproduced

One file, two chunks, all three tables agreeing. Delete one row from `chunks` alone:

| check | clean | after the delete | right answer |
|---|---|---|---|
| `orphans()["fts"]` | 0 | **0** | 1 |
| `orphans()["vec"]` | 0 | 1 | 1 |
| `MATCH 'hello'` | 2 rowids | **3 rowids** | 2 |

The third row is the damage. The search still returns the dead rowid, and the check that exists to
notice says clean.

A bare `INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')` also says clean here, so it
is not a substitute. The `('integrity-check', 1)` form does see it, on SQLite 3.45.1, but it raises
`database disk image is malformed` and returns no count.

# The fix

Count the index's own rows, from the shadow table:

```sql
SELECT COUNT(*) FROM chunks_fts_docsize WHERE id NOT IN (SELECT id FROM chunks)
```

`chunks_fts_docsize` holds one row per indexed document, and it is not external. Reading a shadow
table is the pattern `store.vector_blocks` already uses for `chunks_vec_vector_chunks00`, so this
adds no new idea. It stays one statement, which matters against the 220-line module ceiling.

# Why it survived

The only coverage was `tests/test_index.py:231`, asserting `{"fts": 0, "vec": 0}` on a store that
was clean. A green-only assertion passes against blind code. The new arm in `tests/test_doctor.py`
builds the orphan and asserts the count, and it was run against the predecessor first: **0 against
1**.

# The population, because zero is not absent

Censused on 2026-09-04 across the whole index directory: **476 store files, 476 read, 0
unreadable.** Real orphans of either kind: **0**. Seven stores reported orphans on the first pass
and all seven were probe stores this session had just written into the live index directory; they
were removed and the unclaimed count returned to its prior 49.

So nothing on this fleet is damaged today. The check was blind to a failure that has not happened,
and the repair is worth its two lines because the check's whole job is the next one.
