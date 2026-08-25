---
type: Defect
resource: src/coderag/index.py, src/coderag/store.py
title: A derived column was stored and never re-derived, so widening LANGS reclassified nothing
description: "`files.lang` comes from the path alone, but it is written once at read time and the content-hash diff never rewrites an unchanged file. Growing LANGS from 40 to 166 extensions reached zero of the 2,058 `.groovy` files already indexed. The no-language bucket only fell because `.svg` was deleted from the index, which made the total look like progress."
tags: [indexing, staleness, verification]
status: stable
generated: { by: claude/opus-5, at: 2026-08-21T00:00:00Z }
---

# The bug

`discover.read` sets `lang=filters.lang_of(rel)` and `store.upsert_file` writes it. Staleness in
this engine is one content-hash diff. That is correct, because it asks whether the store matches
the *disk*. `lang` is not on the disk. It is derived from the path by a table that lives in this
repo. So a file whose bytes never change carries whatever classification the table gave it on the
day it was first read.

`store.incompatible` does not cover it either: it keys on the embedding model and the chunker
settings, both of which govern what is *in* a chunk. Nothing there names the language table, and
nothing should — a language change does not invalidate a vector.

# What it cost, measured

Widening `LANGS` 40 → 166 and adding `FILENAMES` predicted the no-language bucket would fall from
14,639 files to under 3,000. The scan after the reconcile returned **6,506**, and the residue was
exactly the classes the widening was written for:

| | before | after the widening alone |
|---|---|---|
| `.svg` | 8,039 | **0** — deleted, not reclassified |
| `.groovy` | 2,058 | 2,058 |
| `.local` | 1,740 | 1,740 |
| no extension | 1,760 | 1,683 |

Every number that moved moved because the file was *removed* from the index. Not one moved because
it was reclassified. The `.svg` drop is what made the total look like progress, which is the shape
that would have let this ship unnoticed.

# The fix

`index._relang` re-derives `lang` for every stored path at the top of each pass and updates the rows
that disagree. It touches no chunk, so it costs no embedding and no GPU — a `UPDATE files SET lang`
over paths the store already holds.

It runs unconditionally rather than behind a version stamp. A stamp would be a second thing to
remember to bump. The pass is a dictionary comparison over a table that is already being read.

# What generalises

Any column calculated from repo-local rules rather than from file content has this shape, and the
content-hash diff is structurally blind to all of them. `lang` is the only one today. The guard is
the rule, not the column. **if this repo's own tables can change the value, the diff cannot see
it, and something has to re-derive it.**

# The verification lesson

The prediction that caught this was "under 3,000", written before the change and checked after. A
softer claim — "the no-language bucket should shrink" — is satisfied by 6,506, and the defect
survives. See [the ignore list only ever matched at the root](the-ignore-list-only-ever-matched-at-the-root.md),
found the same way.

Then the fixed number cleared the prediction at 2,077, and that reading was also wrong. 1,740 of it
was `.local` mapped to `ini` — nginx server blocks, an extension in no linguist language, added in
the same commit. The true figure is **3,782** and the prediction misses. Both errors point one way:
a number was accepted because it landed on the side of the threshold that ended the check. The scan
confirms a prediction. Only a diff against the upstream source confirms a table.
