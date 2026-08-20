---
type: Defect
resource: src/coderag/search.py, tests/test_search.py
title: The pre-rerank cut ranked 136 projects by scores that are only meaningful within one
description: "RRF fuses lanes inside a project, so its scores are per-project ranks: every project's rank-1 hit scores about the same. Truncating the flat pool to CANDIDATES by that score keeps roughly everyone's top one and drops the caller's own rank-3 hit before the reranker sees it — and with rerank=False that order is final."
tags: [search, federation, ranking, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-08-20T23:40:00Z }
---

# Invisible at one project, load-bearing at 136

Both halves of this are unreachable until a root federates. The fleet has exactly one root that
does, claiming 135 members, so the production federated path is also the only place these run.

**The cut.** Reciprocal rank fusion is computed per project. Its output is a rank, not a
comparable relevance, so a flat sort across projects orders by "how highly did its own project rank
it" — and a 60-slot pool over 136 projects is filled by everyone's first hit. Normalizing the
scores cannot fix it and was refuted on that basis: every project's rank-1 already scores the same,
so normalization is a no-op. A quota does. `_pool_cut` gives the caller's own root
`max(1, limit // 2)` slots, round-robins the members through the rest, and hands leftovers back to
the root.

**The dedup.** `_diversify` fingerprinted `(rel_path, text[:400])` while the per-file cap keyed on
`(project, rel_path)`. An identical chunk in both the caller's repo and a member collapsed to
whichever ranked higher, so a caller could be shown a *member's* path for code sitting in their own
tree — the common case on this fleet, with vendored copies and worktree checkouts. The root now
owns its fingerprints: a member hit whose fingerprint the root already claimed is skipped, not the
other way round.

# Nothing asserted anything about merge order

The one existing cross-store test checked only that a member's hit appears at all, which both
defects satisfy. The three new tests were each verified red against a restored copy of the previous
`search.py` before they were kept.
