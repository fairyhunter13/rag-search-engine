---
type: Constraint
resource: tests/eval.py, tests/test_eval_harness.py
title: An eval query is removed from the corpus it is hunting, and the corpus contains what the arm changes
description: Leaving the lead block in the indexed copy makes every arm score near 1.000 on string identity, and an arm whose file type is absent from the query set cannot be read at all — the two ways this harness lies quietly.
tags: [evaluation, retrieval, methodology]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T22:05:00Z }
---

# Two ways the harness reports a number that means nothing

**The query must not survive in its positive.** `eval.py` derives each query from a file's leading
block — a docstring for code, the first heading plus its lead paragraph for prose — and `materialize`
writes a copy of the corpus with that block cut out. Skip the cut and the query is a verbatim
substring of the chunk it is looking for: BM25 matches it exactly, every arm lands near 1.000, and
the ranking has collapsed to string identity while still printing four decimals. This is the
CodeSearchNet protocol and it is the whole reason the copy exists.

**And the arm has to be visible to the query set.** `--corpus code` excludes doc-langs on purpose —
a markdown H1 matches the line-comment pattern, so including them would measure prose recall under a
name that says code. The cost went unstated for a while: it also made every prose-side change
unmeasurable. `--corpus docs` is the other half, disjoint by construction, and the two are comparable
arm-to-arm within one corpus and never to each other.

Both are asserted in `test_eval_harness.py`, in one test each covering both directions — a query
built but not stripped is the flattering failure; a block stripped but never queried leaves the mode
with no query set at all, and each half passes alone while the harness is broken.

# What the doc protocol accepts and refuses

The **first** heading, not the first H1: plenty of files here open at `##`, and requiring `#` selects
for the author's habit rather than for the text. A heading with no lead paragraph is **refused**, not
padded out — 16 of 33 doc files in the sibling repo are a title followed straight by a sub-heading,
and a three-word title as a query measures title-to-filename matching. That is why the measured
yield is 18/19 here and 17/33 there, against an earlier estimate of 82%: the honest number, and it
still clears the query count the fleet needs by two orders of magnitude.

Per-query ranks now ride along with the aggregates. Two arms 0.02 apart in recall@10 differ by about
seven queries in three hundred, and a bootstrap CI or McNemar over the disagreements needs the paired
outcome — which `score()` computed and discarded, leaving every arm comparison an eyeball on four
decimals of an unpaired mean.

Read the deltas within a run, never the levels: the same model scored 0.61 and 0.19 on two stores of
the same corpus, purely from distractor count. See
[[the-header-dispatches-on-type-the-splitter-does-not]] for what this instrument was built to settle.
