---
type: Decision
resource: src/coderag/chunk.py, src/coderag/filters.py
title: Per-type knowledge enters through the header, not through a second splitter
description: A quarter of the corpus is prose and structured data that the code-only evidence never covered. The fix is four arms inside scope_header, because that is reversible and a second chunker costs a full re-index to compare.
tags: [chunking, retrieval, evidence, prose]
status: deprecated
generated: { by: claude/opus-5, at: 2026-08-19T21:10:00Z }
sources:
  - id: structured-data-chunking
    resource: https://arxiv.org/abs/2605.00318
    title: "arXiv:2605.00318 - structure-aware handling of structured data, Tables III and IV"
    author: team:arxiv
---

# The gap

`chunk.py` imported `TextSplitter` and nothing else, built one instance for every file, and
`chunk_text` had no `lang` parameter. The wheel also ships `MarkdownSplitter` and `CodeSplitter`;
neither was referenced. Meanwhile `scope_header` was three arms of regex tuned to the C family,
Python, Go, Rust and PHP. Applied to every file regardless of what it was.

That is defensible on the corpus the chunker decision was argued over and silent on the real one.
Across the 148 enabled fleet roots, 174,132 tracked files: **6.3% doc-langs, 9.4% structured data,
25% carrying no `lang` label at all**. In this repo plus ccw, markdown is a quarter of all chunks.
Every source under [one-chunker-and-it-is-third-party](one-chunker-and-it-is-third-party.md) — RepoEval, CrossCodeEval, cAST,
arXiv:2605.04763 — is a code benchmark.

# The decision

The **header** dispatches on `filters.lang_of()`. The **splitter** does not.

| lang | `in:` line |
|---|---|
| code | the nearest column-0 declaration — unchanged |
| `DOC_LANGS` | the enclosing heading chain, `coderag > Per-project config > Excludes` |
| `DATA_LANGS` (json, yaml, toml) | the enclosing key path, `jobs.quality.runs-on` |
| unlabeled | none; the path alone |

Dispatching here is the cheap, reversible half. The header is one string prepended before
embedding, so changing it costs a re-index and nothing else. A second splitter multiplies the eval
matrix by its own arity and doubles the store, over the axis the evidence rates lowest.
`MarkdownSplitter` measured better on boundaries: 0 unbalanced code fences and 0 mid-table starts,
against 4 and 2.

But it made 30% more, smaller chunks. Fragmentation is exactly what sank the semantic chunker in
the decision above. So it was a bake-off arm, not an edit.

**and the arm has now run and lost**. It scored 0.8033 against 0.8133 recall@10 over 300 doc
queries, −0.0100, CI [−0.0300, +0.0100]. 3 queries won against 6 lost, p = 0.51. Not distinguishable, so
`CHUNK_MD_SPLITTER` stays off and one chunker ships. Fixing every broken code fence and mid-table
cut bought nothing a retrieval metric can see, which is the more useful half. The boundaries a
reader would call wrong are not the boundaries that lose queries.

The one large *measured* effect in this literature is on structured data. Structure-aware handling
gave Recall@1 0.366 → 0.754 and MRR 0.372 → 0.776 over a recursive character baseline
([arXiv:2605.00318](https://arxiv.org/abs/2605.00318), Table IV, BM25-only lane. The hybrid lane in
Table III is 0.347 → 0.539). **Two corrections to how this file first cited it**, both found by
re-reading the paper rather than the summary of it:

- it is **not measured on CSV or Excel**. Those are the abstract's motivating domain. The
  benchmark is MAUD, SEC merger agreements reshaped into row/key-value form. So the surrogate is
  further from JSON/YAML/TOML than "spreadsheets" suggested, not closer;
- the delta is **not attributable to prepending a key path**. It bundles a row tree, key-value
  block encoding and greedy merging, and cuts chunk count 40–56% along the way — some mechanisms,
  one number.

What survives is a direction, not a magnitude: structure-aware handling of structured data is worth
measuring. The `in: <key path>` arm is this repo's cheapest test of that direction and its result
will be its own. Labeled a prior rather than a result for the reason
[one-chunker-and-it-is-third-party](one-chunker-and-it-is-third-party.md) records. This bundle has
already had to correct one citation that claimed more than its source said, and this is the
second.

# What it replaces, and what it measures

It subsumes the guard that would otherwise have been patched in beside it. `_DECL`'s generic
C-family arm matches any prose line ending in a parenthetical, so a markdown chunk could inherit
`in: Five Claude Code profiles (…)` from an unrelated earlier section. 2 of 142 prose chunks,
small but the worst kind of wrong, since a topically adjacent incorrect header is the hardest
distractor to retrieve past. A prose header built from headings cannot be built from `_DECL` at all.

Measured after, through `discover.candidates()` + `read()` over both dev repos. Never a bare
`rglob`, which is what produced a false 79% reading by reaching into `.venv`:

| kind | chunks | with `in:` |
|---|---|---|
| code | 344 | 219 (64%) |
| doc | 124 | 77 (62%) |
| data | 7 | 5 (71%) |
| unlabeled | 11 | 0 |

and **0 prose or data headers not traceable to a real heading or key in their own file**. The
assertion that matters, because "has an `in:` line" is satisfied by a fabricated one.

An unlabeled extension falls to the code arm on purpose. `indexable()` is a denylist, so a `.zig` or
`.mojo` file is discovered, chunked and searchable today with no change. The generic declaration
regex picks up most curly-brace declarations for free and degrades to the path. The absence of a
parser is what makes that true, which is why a future "add tree-sitter" proposal has to pay for it.

# Refuted — the arms ran and the derived line lost

`--corpus docs` and then the code 2x2 both measured it flat, so `scope_header` dispatches on nothing
now and the four arms below are deleted. See [the-header-is-the-path-and-nothing-else](the-header-is-the-path-and-nothing-else.md) for the
cells, and for the census that refuted the redundancy explanation. The section that follows was the
falsification condition this file set for itself, and it is the one that fired.

# Still unfalsifiable, and that is the next step

`tests/eval.py` excludes `DOC_LANGS` from its query set deliberately. A markdown H1 matches the
line-comment pattern, so including it would measure prose recall under a name that says code. So
every arm above is currently unmeasurable on the thing it changes. `--corpus docs` is the
instrument: H1 plus lead paragraph as the query, heading stripped from the indexed text, 82% yield
over 50 doc files.

`docs-header` and `md-splitter` are arms once it exists. If they lose or tie, the path-only prose
header and the one-splitter decision come back **strengthened**. The test then ran on the corpus
they were never argued over.

Recorded and not built: notebook extraction (code cells only, outputs dropped). `.ipynb`, `.csv`
and `.tsv` are in `DEFAULT_IGNORES` instead. Indexed as raw JSON a notebook yields no retrievable
chunk under any splitter, and per-type chunking would only make the garbage tidier.

Also recorded: `lexical.py` tokenises identifiers and returns `[]` for ordinary lowercase English,
so a prose chunk competes on one column where code competes on two. That wants a number before it
wants a change, and `--corpus docs` is what produces the number.
