# Info Hierarchy

OSE's DIKW doctrine ladder — how data climbs to wisdom and what each rung costs.

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

## Extraction / semantic-resolution ladder (HR15–HR19, HR23)

The **Information** step above is itself a confidence-gated ladder, not a single pass: tree-sitter
structure (token-zero) resolves the majority; GPU rerank (token-zero) resolves residual ambiguity by
structural context; DeepSeek (capped/cached/batched, SEA select-not-author) resolves only what remains.
No regex, static/dynamic keyword list, or mapping table may substitute for a tier — surface-text name
matching is unsound (false positives) and is banned for semantic inference in Category A
(`kb/bpre*.py`, `kb/patterns.py`, `server/_overview.py`; see §7a of
`docs/architecture/federation-and-search-engine.md`). **Token frugality is the enforcement
complement**: every DeepSeek call in this ladder must run behind a stable prefix (cache), be batched,
be capped, receive structural context (not bare names), and select from an admitted candidate set —
and its usage must feed `llm_token_stats()` (HR23) so the budget is auditable. As of 2026-07-01 this
accounting covers both narration (`bpre.*`) and edge-resolution (`bpre_link`) DeepSeek calls; any new
call site in `kb/` or `graph/` must do the same (L4 pattern in `model.yaml`).

## Compute-spend doctrine (CPU / GPU / RAM)

Parallel to the LLM-spend ladder above, OSE applies a **compute-spend doctrine** governing when CPU, GPU, and RAM are consumed:

- **Spend compute only to re-climb the DIKW ladder on real source drift.** The heavy cascade (enrich/wiki/BPRE) runs only when `_source_fingerprint` detects that indexed source files actually changed. Metadata-only events (file close/open, CHMOD) and changes to non-indexed files are filtered at the watcher boundary and again by the source-drift gate in `on_change`.
- **Idle ⇒ near-zero CPU + constant RAM floor.** With no queries and no source drift the daemon holds < 1 % CPU. The existing `_idle_unload` path (300 s default) nulls the ONNX session references, calls `gc.collect` + `malloc_trim`, and releases the ORT CUDA arena — the only reliable way to return GPU memory to the OS. Models reload on demand at the next real query or edit.
- **GPU is the sole inference engine — maximized, never idle-spun.** Embedding and reranking run exclusively on CUDA; CPU fallback is fatal. The GPU is not used during idle periods; it warms up only for actual embed/rerank operations triggered by real queries or real source changes.
- **File-watching uses kernel notifications, not CPU polling.** `watchfiles` (Rust `notify`) is the watching mechanism — push events from the OS kernel, not a polling loop, delivered through a **single** background thread + inotify instance covering every watched root. The poll fallback lives inside the Rust library (`force_polling`, NFS/SMB/WSL) — there is no hand-rolled Python poll loop to maintain.
- **The watcher trigger surface must be ignore-aware too, not only the gates downstream of it (HR37, 2026-07-01 evening).** A drift gate that correctly ignores tool-cache churn is not enough if the *watcher* still receives, buffers, and dispatches every raw event from that churn before the gate ever gets a say. `daemon/watcher.py`'s `watch_filter` reuses the same HR35 `is_ignored_path` resolver as the drift gate, and Rust-side debounce/step coalesces bursts before they cross into Python — closing a 4th, distinct idle-CPU cause where a per-root watchdog Observer (one inotify instance + thread *per project*, ~278 threads on a 139-member fleet) delivered every raw ignored-dir event individually into an unbounded Python buffer, pinning one dispatch thread even after the HR35/HR36 gates were already reporting "no drift" correctly.
- **The drift gate's input must itself be trustworthy (HR35, 2026-07-01).** `_source_fingerprint` walks via `iter_files`/`is_ignored_path` (`index/discover.py`), which apply one shared discovery decision order: OSE `.opencode-index.yaml` `exclude` (drop) > OSE `include` (force-keep) > default hidden-dir/`IGNORED_DIRS` policy (drop) > `.gitignore` (drop, supplementary, cached per-mtime) > keep. Gitignored/hidden tool-cache dirs (`.svelte-kit`, `.playwright-mcp`, `.astro`, `.turbo`, `.vite`, etc.) never enter the fingerprint, so a live dev-server/tool-cache rewriting those paths cannot spuriously flip the sig and re-trigger the heavy cascade — root-caused after a live `vite dev` + Playwright-MCP session pinned a CPU core via an every-~5min false-positive BPRE/enrich rebuild.
- **Every compute-spend gate must be scoped to what it actually consumes, not just what's easiest to hash (HR36, 2026-07-01).** BPRE's federation-wide reuse stamp (`bpre_source_sig`) and per-member scan-cache key (`_member_scan_sig`, `kb/bpre.py`) are **code-only**: `_source_files` routes through the same HR35 `_should_drop` resolver as `iter_files` (hidden-dir/gitignore/OSE-config aware, `is_code_language`-gated), and `_bpre_code_sig` hashes only that file set — never the all-files `_source_fingerprint`. The stamp is written once from the sig computed at rebuild start (no end-of-rebuild recompute chasing a moving target). Generalizes HR35: a drift gate reusing a *coarser* signature than its actual dependency surface will spend compute on irrelevant churn — root-caused as a 3rd, distinct idle-CPU cause (2026-07-01) when docs/config/image edits and `.claude/*.js` tool-cache churn on a 170-member federation root kept flipping the all-files stamp faster than the ~5min federation rebuild it triggered could complete, pinning a CPU core continuously even after HR35 shipped.

## Publishability & device-neutrality (P18, HR34)

OSE is a **public repo**. Parallel to the compute-spend and extraction doctrines above, every
tracked artifact — source, tests, docs, scripts, generated wiki/docgen/OKF output — must be safe to
publish: no secrets, no real device paths, no company/project names. This is a whole-repo widening
of P7/HR13 (which already banned absolute paths in generated wiki/docgen/OKF artifacts specifically).
Device/host portability is achieved the same way efficiency is achieved elsewhere in this doctrine —
by never hardcoding what should be resolved at the boundary: every machine-specific value (storage
paths, host, port, embed/rerank models, GPU device) is **env-driven with an XDG-style default**
(`core/config.py`), so the same tracked tree runs unmodified on any machine. Guarded by
`test_public_hygiene.py` (whole tracked-tree scan for `/home/`, `/root/`, `/Users/`, and Windows
`C:\Users\` literals, plus a structural check that `core/`/`daemon/` storage-path constants derive
from `os.environ.get(...)` rather than a hardcoded literal) and `test_no_real_project_in_tests.py`.
Device-specific name bans (real company/codename/device-id lists) are intentionally kept out of this
public tree and live only in the private `ose-live-audit` repo.

## Hierarchy removal (WS-B, 2026-06-26)

The former L2 (domain aggregations) and L3 (federation themes) layers between Knowledge and Wisdom have been **deleted**. They added 35,000+ graph.db rows per project at significant LLM cost but were not consumed by any query path that flat-L1 couldn't serve. Standalone docgen/OKF tools (WS-A/WS-C) now own deep hierarchy generation for any repo — they parse the repo directly, with no OSE graph.db input.

## How to use

- **search/ask/overview** — consumes Data+Information+Knowledge rungs.
- **overview(what='business_rules')** — Knowledge layer (semantic_type='business_rule').
- **overview(what='process_flows')** — Information+Knowledge (BPRE from tree-sitter+DeepSeek).
- **check_world_model.py** — enforces Wisdom layer against working-tree diffs.
- **gen_world_model_skills.py** — renders `.claude/skills/` from this file + `model.yaml`.

_Source: `docs/info-hierarchy.md` — generated by scripts/gen_world_model_skills.py._
