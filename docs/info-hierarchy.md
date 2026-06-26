# OSE Information Hierarchy — DIKW Doctrine Ladder

> "Spend LLM tokens only to climb Information→Knowledge→Wisdom, and only at the
> nodes/queries actually read." — §1a P1 / HR23

## The ladder

```
WISDOM    §1a Principles (P0–P11) + §13b HRs — the governing laws.
          Derived from architecture decisions across all projects.
          Surfaced as: CLAUDE.md invariants, docs/world-model/model.yaml L1.
          Generation: human-authored + machine-verified (check_world_model.py).
          LLM cost: $0 (pre-built; checked at edit time, not query time).

KNOWLEDGE Community summaries + semantic types (L1, level=1 in graph.db).
          Derived from: symbols + edges → fastgreedy community detection → DeepSeek narration.
          Surfaced as: overview(communities), wiki community_*.md, ask() Architecture section.
          Generation: enrich_communities_batch (DeepSeek, prefix-cached, significance-gated head).
          LLM cost: significance-gated (member_count≥8 OR ≥2 cross-community edges); tail abstains.

INFORMATION Symbols + call edges (graph.db symbols/edges tables).
            Derived from: tree-sitter parse of source files.
            Surfaced as: graph() callers/callees/impact, overview(import_cycles), BPRE.
            Generation: extract_symbols() + detect_communities() — zero LLM, deterministic.
            LLM cost: $0 (structural parsing only).

DATA      Source code chunks + file tree.
          Derived from: iter_files() + chunk_file() with cAST structural-path header.
          Surfaced as: search() results, ask() Code section.
          Generation: index_project() → VectorStore (sqlite-vec, FLOAT[768]).
          LLM cost: $0 (embed-only, GPU).
```

## OSE's DIKW spend doctrine

1. **Data** (embed+index): GPU-only. Never generative. `index_project()`.
2. **Information** (symbols+edges): tree-sitter only. Never generative. `extract_symbols()`.
3. **Knowledge** (community summaries): DeepSeek, significance-gated, prefix-cached. `enrich_communities_batch()`. Abstain on tail (reject-option doctrine, `narrated=0`).
4. **Wisdom** (invariants/principles): authored once, machine-checked. `check_world_model.py`.

## Hierarchy removal (WS-B, 2026-06-26)

The former L2 (domain aggregations) and L3 (federation themes) layers between Knowledge and Wisdom have been **deleted**. They added 35,000+ graph.db rows per project at significant LLM cost but were not consumed by any query path that flat-L1 couldn't serve. Standalone docgen/OKF tools (WS-A/WS-C) now own deep hierarchy generation for any repo — they parse the repo directly, with no OSE graph.db input.

## How to use

- **search/ask/overview** — consumes Data+Information+Knowledge rungs.
- **overview(what='business_rules')** — Knowledge layer (semantic_type='business_rule').
- **overview(what='process_flows')** — Information+Knowledge (BPRE from tree-sitter+DeepSeek).
- **check_world_model.py** — enforces Wisdom layer against working-tree diffs.
- **gen_world_model_skills.py** — renders `.claude/skills/` from this file + `model.yaml`.
