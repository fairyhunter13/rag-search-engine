# The two levers are additive, and the bill is a fleet rebuild

`2026-07-31-the-prefix-is-a-precondition-not-a-tuning-knob.md` closed the question of whether the
prefixes matter — they are the whole of the challenger's margin — and left one cell of the grid
unmeasured: **nomic-v1.5 with prefixes at `RSE_EMBED_MAX_TOKENS=512`**. That is the cell a switch
would actually ship, because 512 was independently the stronger token budget and the prefixes were
independently the stronger model treatment, and nobody had run both at once.

It is now measured, on both projects. It wins every metric against every same-chunking control. The
two levers turn out to be additive to within ±0.015 recall@1. And the switch is still not a config
edit.

## The grid

Retrieval eval is `scripts/eval_retrieval.py`, dense lane, ground truth derived from tree-sitter
symbols. **Only same-`max_tokens` rows are comparable**: `RSE_EMBED_MAX_TOKENS` also drives
`index/chunker.py`, so changing it re-chunks the store and moves the ground truth with it. The
harness says so in its own docstring; the correction below is what happens when you forget.

This repo, 229 files / 130 queries:

| arm | chunks | recall@1 | MRR | nDCG@10 | recall@10 |
|---|---:|---:|---:|---:|---:|
| jina @768 — *production incumbent* | 1,346 | 0.5923 | 0.6890 | 0.7328 | 0.8692 |
| nomic+prefixes @768 | 1,346 | 0.6615 | 0.7375 | 0.7716 | 0.8769 |
| jina @512 — *control* | 2,165 | 0.7231 | 0.7764 | 0.8027 | 0.8846 |
| bge-base-en-v1.5 @512 | 2,165 | 0.7846 | 0.8411 | 0.8650 | 0.9385 |
| **nomic+prefixes @512** | 2,165 | **0.8077** | **0.8587** | **0.8802** | **0.9462** |

A 2,464-file php+javascript+vue project, 200 queries:

| arm | chunks | recall@1 | MRR | nDCG@10 | recall@10 |
|---|---:|---:|---:|---:|---:|
| jina @768 — *production incumbent* | 21,194 | 0.1900 | 0.2766 | 0.3283 | 0.4950 |
| nomic+prefixes @768 | 21,194 | 0.2650 | 0.3510 | 0.3984 | 0.5500 |
| jina @512 — *control* | 31,428 | 0.2350 | 0.3062 | 0.3518 | 0.5000 |
| bge-base-en-v1.5 @512 | 31,428 | 0.2350 | 0.3352 | 0.3920 | 0.5750 |
| **nomic+prefixes @512** | 31,428 | **0.3000** | **0.3853** | **0.4318** | **0.5800** |

**Prefixed nomic @512 wins 8 of 8 metrics against both 512 controls on both projects**, and displaces
`bge-base-en-v1.5` — which the previous round named the strongest arm on this repo — from that
position. End to end, production jina@768 → nomic+prefixes@512 is **+0.2154 recall@1 (+36%
relative)** here and **+0.1100 (+58% relative)** on the larger project.

## The levers are additive, which was not the assumption

The reason this cell had to be run rather than inferred is that the prior explicitly allowed the two
gains to overlap: a smaller token budget and a task-prefixed model might be two ways of buying the
same thing. Decomposing recall@1 from the shared jina baseline within each token bracket:

| | prefix lever | token lever | sum | actual combined | residual |
|---|---:|---:|---:|---:|---:|
| this repo | +0.0692 | +0.1308 | 0.2000 | **+0.2154** | +0.0154 |
| the larger project | +0.0750 | +0.0450 | 0.1200 | **+0.1100** | −0.0100 |

Slightly super-additive on one project, slightly sub-additive on the other, both inside ±0.015 —
i.e. **independent within the resolution of a 130/200-query eval.** Neither lever is a substitute for
the other, and the combined arm is not double-counting a single effect. That is the result that makes
the grid worth acting on rather than re-running.

## A correction: the control moved because the corpus did

The first pass of the 512 arms on this repo scored jina@512 at **0.7385**. The re-run above scores
the same arm at **0.7231**. The arm did not change; the repo did — it indexed 229 files / 2,165
chunks against the recorded control's 224 / 2,118, because decision docs were added to `docs/` on the
same day the earlier arms were recorded.

A 0.015 drift is the same size as the additivity residual this doc is trying to resolve, so it is not
ignorable. **Both 512 controls were re-run on the current corpus** and the table above is entirely
within one corpus snapshot. The larger project's corpus was verified *unchanged* — its new arm
rebuilt to exactly 2,464 files / 31,428 chunks, matching the recorded controls — so its stored 768
and 512 rows stayed valid and were not re-run.

The general rule, which the harness docstring states and which this is the incident for: **a control
recorded against a different corpus is not a control.** Re-derive it or re-run it; do not compare
across the boundary.

## What a switch costs

The margin is unreachable from where the code stands, and the shape of the blocker is small and
precise. `Embedder.embed` takes texts and nothing else — it is not told which side of the retrieval
it is serving, and the prefixes differ by side. Four production call sites, split cleanly:

| side | call site |
|---|---|
| document | `index/indexer.py:84`, `index/indexer.py:172` |
| query | `query/search.py:114`, `query/search.py:238` |

`index/chunker.py`'s `_tokenizer()` needs no edit at all: it already does
`Tokenizer.from_pretrained(EMBED_MODEL)` against the *config* value, so it follows the model
automatically. That is also the coupling that makes this expensive — changing the model or the token
budget re-chunks every store, which is exactly what `AUTO_MIGRATE_VECTORS=0` exists to prevent from
happening silently.

So the bill is not a re-embed, it is a **full rebuild of 150 stores through the GPU**: parse, chunk,
embed. Measured from the one full-project arm that was timed end to end — 31,428 chunks plus 200
eval queries in **585 s**, i.e. ~54 chunks/s including parse and chunking:

| | |
|---|---:|
| fleet chunks today (768) | 302,594 across 150 stores |
| chunk inflation at 512 | ×1.61 here, ×1.48 on the larger project |
| projected fleet chunks at 512 | ~450,000 |
| projected wall clock | **~2.4 h** |

Treat that as an order of magnitude — one project's language mix is not the fleet's — but the
conclusion survives any plausible error bar: this is **hours of GPU, not the 21.6-minute CPU
re-derive** an extractor stamp move costs. It cannot ride along with a graph change, and it wants a
window where nothing else needs the box.

## Status

**No switch in this commit.** `AUTO_MIGRATE_VECTORS` stays `0` and the incumbent stays jina. What
changed is that the decision is now fully costed on both sides: the evidence is 8/8 on two projects
with the levers shown independent, and the price is an asymmetric embed path across four call sites
plus a multi-hour fleet rebuild.

The `_Prefixed` wrapper stays in `scripts/eval_retrieval.py` and deliberately does **not** move into
`embed/embedder.py`. Putting it there would be the asymmetric-embed change, made without the call
sites that give it its argument — a document-side prefix applied to a query, which is the exact
failure mode the previous doc measured as the whole margin, inverted.

## Addendum — 2026-08-01: what the two query sets were made of

The arm result files carry a `query_languages` field that no table above reproduces. It is reported
for a reason the harness states in place: *"a set that has quietly collapsed to one language is the
exact failure this selector replaced, and it would otherwise look like a normal result."* Read back
off the stored arms before they were deleted, and identical across every arm within each project:

| | query set |
|---|---|
| this repo, 130 queries | `python 118, go 11, html 1` |
| the larger project, 200 queries | `php 61, python 61, vue 32, javascript 30, scss 10, bash 4, sql 1, typescript 1` |

**This repo's table is 91% a Python result.** It has not collapsed to one language, but 118 of 130 is
most of the way there, and the engine supports nine. So the "wins 8 of 8 metrics on both projects"
claim decomposes unevenly: the larger project's 200 queries are the multi-language evidence and the
load-bearing half of the grid, and this repo's 130 are close to a single-language confirmation of it.
That does not overturn the additivity result — the residual is computed within each project, not
across them — but any future arm that wins here and loses there should be read with this in hand
rather than treated as a tie.

The two figures also explain the absolute-level gap the previous doc attributed to store size alone.
Vue and SCSS are out-of-distribution for the code-embedder line (CodeSearchNet covers Go, Java,
JavaScript, PHP, Python and Ruby), and they are 21% of the larger project's queries and 0% of this
repo's.

**The arm stores are gone.** All twenty arm `vectors.db` trees — the six in the first pass, the four
on the larger project, the six re-runs, and four from an earlier round — were deleted on 2026-08-01,
after every metric in both tables above was read back out of them and reconciled line by line. Their absence is not
missing work: rebuilding any arm is one `scripts/eval_retrieval.py build` run, ~585 s for the larger
project. Note also that the first-pass numbers this document corrects (jina@512 at 0.7385) came from
a superseded corpus and were deliberately **not** carried forward.
