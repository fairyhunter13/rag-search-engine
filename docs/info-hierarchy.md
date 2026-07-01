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

## Compute-spend doctrine (CPU / GPU / RAM)

Parallel to the LLM-spend ladder above, OSE applies a **compute-spend doctrine** governing when CPU, GPU, and RAM are consumed:

- **Spend compute only to re-climb the DIKW ladder on real source drift.** The heavy cascade (enrich/wiki/BPRE) runs only when `_source_fingerprint` detects that indexed source files actually changed. Metadata-only events (file close/open, CHMOD) and changes to non-indexed files are filtered at the watcher boundary and again by the source-drift gate in `on_change`.
- **Idle ⇒ near-zero CPU + constant RAM floor.** With no queries and no source drift the daemon holds < 1 % CPU. The existing `_idle_unload` path (300 s default) nulls the ONNX session references, calls `gc.collect` + `malloc_trim`, and releases the ORT CUDA arena — the only reliable way to return GPU memory to the OS. Models reload on demand at the next real query or edit.
- **GPU is the sole inference engine — maximized, never idle-spun.** Embedding and reranking run exclusively on CUDA; CPU fallback is fatal. The GPU is not used during idle periods; it warms up only for actual embed/rerank operations triggered by real queries or real source changes.
- **File-watching uses kernel notifications, not CPU polling.** `watchdog`/inotify is the primary watching mechanism — push events from the OS kernel, not a polling loop. The poll fallback (`_loop`) activates only when inotify is unavailable (NFS/SMB, `max_user_watches` exhaustion) and seeds a baseline on its first pass rather than scanning eagerly.

## Hierarchy removal (WS-B, 2026-06-26)

The former L2 (domain aggregations) and L3 (federation themes) layers between Knowledge and Wisdom have been **deleted**. They added 35,000+ graph.db rows per project at significant LLM cost but were not consumed by any query path that flat-L1 couldn't serve. Standalone docgen/OKF tools (WS-A/WS-C) now own deep hierarchy generation for any repo — they parse the repo directly, with no OSE graph.db input.

## How to use

- **search/ask/overview** — consumes Data+Information+Knowledge rungs.
- **overview(what='business_rules')** — Knowledge layer (semantic_type='business_rule').
- **overview(what='process_flows')** — Information+Knowledge (BPRE from tree-sitter+DeepSeek).
- **check_world_model.py** — enforces Wisdom layer against working-tree diffs.
- **gen_world_model_skills.py** — renders `.claude/skills/` from this file + `model.yaml`.
