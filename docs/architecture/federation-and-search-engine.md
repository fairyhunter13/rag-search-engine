# Federation & Search-Engine Architecture — Part 1: Core

> Source-of-truth is `src/opencode_search/`. Last reconciled 2026-06-20 (BPRE Phase D — process_graph.db + HR14 added; §8b; codex removed → haiku-only chat HR10; direct-DeepSeek classifier HR11; `think=False` no-idle-spin HR12; LLM lanes split D/E in §8a/§9b).
> Continued in [federation-ops-and-invariants.md](federation-ops-and-invariants.md).

## 1. Purpose & scope

opencode-search is a local, GPU-only semantic code-search and KB engine. It indexes one or
more project trees and serves five MCP tools (`search`, `ask`, `graph`, `overview`,
`index`) plus an HTTP dashboard from a single daemon at `127.0.0.1:8765`.

**Federation** treats a *root* project that contains **symlinks to external sub-repos** as
one **logical repository**, while storing and indexing each linked sub-repo ("member") as
an independent unit.

## 2. Vocabulary

| Term | Meaning |
|---|---|
| **Root** | A registered project whose tree contains symlinks pointing outside itself. |
| **Member** | An external repo reached via a symlink under a root; stored as its own project. |
| **Logical repo** | Union of root + all its members. |
| **Index dir** | `INDEX_ROOT/{slug}-{sha256(path)[:16]}` holding `vectors.db`, `graph.db`, `wiki/`. |

## 3. System context

- **Daemon** (`daemon/server.py`): uvicorn app = `mcp.streamable_http_app()` (FastMCP) +
  dashboard routes. Boots `assert_cuda_available()` first — **CPU fallback is fatal**.
- **Background** (`_start_background`): Scheduler → synchronous `register_all_members()` →
  `start_watcher()` → one-shot `reconcile_projects` thread.
- **Registry** (`core/registry.py`): `~/.local/share/opencode-search/projects.json`,
  atomically written under `fcntl` lock. Each row: `ProjectEntry` with
  `path, enabled, indexed_at, file_count, chunk_count, federation: list[str], …`.
- **Vector store**: sqlite-vec flat `vec0`, `FLOAT[768]`, exact recall.
- **Graph store**: SQLite `symbols / edges (caller_sid, callee_sid) / communities`.
- **Enrichment LLM**: local `ollama qwen3-enrich:1.7b` (summaries, GPU, `think=False`) + cloud
  **DeepSeek** `deepseek-chat` (semantic-type classification + Phase B wiki narrative). The query
  LLM (**claude-haiku-4-5** via Claude Code CLI) is **dashboard-chat only** — never called by MCP
  tools. **Codex support removed.**

## 4. Federation discovery (`daemon/federation.py:discover_members`)

```
root = Path(root_path).resolve()
os.walk(root, followlinks=False)       # any depth; do NOT follow links while walking
  prune dirs in IGNORED_DIRS
  for each dir that IS a symlink:
     target = dir.resolve()
     skip if target == root or target.is_relative_to(root)   # cycle guard
     if _looks_like_repo(target):      # iter_files(target) yields ≥1 file
         members.append(str(target))
     dirs.remove(dir)                  # never descend into the symlink
```

- **Any-depth** scan (commit 2796ae6): nested symlinks found, not just direct children.
- **Cycle guard**: links resolving back inside root are ignored.
- Returns **resolved absolute paths** so a member has a canonical identity.

## 5. Registration model

- `index_members(root)`: discover → upsert new members as `enabled=True` → write
  `root.federation = [all members]`. Returns newly-registered count.
- `register_all_members()`: `index_members` for every enabled project; idempotent.
- `expand_federation(path)`: `[path] + entry.federation` — the canonical "whole logical
  repo" primitive used by cascade-remove and read-path aggregation.

**Members are first-class independent projects.** Each has its own index dir and is
independently searchable. The root merely *references* members in `federation`.

## 6. Storage & isolation

Content-addressed: `INDEX_ROOT/{slug}-{sha256(path)[:16]}`. No cross-project DB sharing.
**Orphan vacuum** (`sweeps.maintenance`, @6 h): any `INDEX_ROOT` subdirectory not in the
registry is `rmtree`'d.

## 7. Indexing pipeline (`sweeps._index_project`)

1. **Chunk + embed** → `vectors.db` (`index/indexer.index_project`).
2. **Symbol extraction** (tree-sitter) over `iter_files(root, federation_mode=True)`.
3. **Call-edge resolution** (second pass): cross-file edges only.
4. **Community detection**: Leiden L1.
5. Stamp `indexed_at`, `file_count`, `chunk_count` in registry.

`federation_mode=True` prunes symlink dirs/files pointing **outside** the root — the
no-inlining invariant. Without it a root's file_count balloons ~12× by double-counting
linked trees.

## 8. Enrichment pipeline (`sweeps._enrich_project`)

1. Prune stale L1 communities.
2. Enrich L1 communities with NULL summary (LLM; thermal guard at 80 °C).
3. If L2 absent: `build_hierarchy` (coarse Leiden, √L1 target).
4. Enrich L2 communities with NULL summary.
5. **Classify `semantic_type`** for new/unclassified L1 communities
   (`classify_communities_semantic`, `reclassify_all=False`).
6. `build_wiki(gs, wiki_dir)` — rich bundle: type-grouped `index.md`, deterministic
   `community_{id}.md` (reused summary + root-relative source citations + edge-drawn mermaid),
   `domain_{id}.md` (DeepSeek narrative, templated fallback). No GPU; only L2 narrative is cloud.
7. `build_federated_index(project_path)` + regen of any owning root — writes the federated root's
   `federation.md` (aggregation of each member's own graph.db; no cross-repo edges, HR4). No-op
   for standalone projects. (See Part 2 §13b HR13.)
8. `reconstruct_processes(root_path)` (Phase D BPRE) — runs ONLY for federation roots (≥2 members).
   Writes `{index_dir}/process_graph.db` containing D2 entry points, D3 cross-service edges
   (gRPC+Pub/Sub+HTTP), D4 process traces, D5 rules/state machines, D6 BPMN+mermaid+narrative.
   **GPU-free** for D2-D4 + BPMN/mermaid; cloud DeepSeek for D5 rule text + D6 narrative only.
   (See §8b and **HR14** below.)

All enrichment is **idempotent and gated on `summary IS NULL`** (classification gated on
`semantic_type IS NULL OR non-canonical`), so the daemon never re-labels settled communities.

### 8a. LLM lanes within enrichment (resource-critical)

- **Summaries + symbol intents** (steps 2, 4): local `ollama qwen3-enrich:1.7b` via
  `graph/llm.py:chat(think=False)`. **`think=False` is mandatory** — qwen3 is a thinking model;
  its `<think>` output otherwise grows into the 4096 context → Ollama truncates → `llama-server`
  busy-spins a core at ~84% indefinitely (Ollama [#13461](https://github.com/ollama/ollama/issues/13461)).
  With the flag: clean bounded output, ~2–10% idle CPU, model unloads at keep_alive idle.
- **Semantic-type classification** (step 5): cloud **DeepSeek** (`deepseek-chat`) over
  title+summary, batch-20 (`_classify_batch` → `_kb_chat`, DeepSeek primary / Ollama fallback).
  A 1.7B local model + cosine-to-centroid mislabeled ~61% of business_rules as test suites;
  DeepSeek separates test-of-business from business logic. Structural guard: a `<3`-member
  community cannot be a multi-step business_process/rule (demoted to `feature`).

## 9. Query / read path (`server/mcp.py`)

- **`search(query, scope, project_paths?)`**: when explicit paths are given, each resolved
  root is expanded through `expand_federation` (dedup), so a root-scoped query fans out
  across all members. No-path branch already covers members (they are enabled projects).
- **`ask(query, project_path?, scope)`**: gathers chunks from all `expand_federation` paths
  (each member's `VectorStore`, top_k per member), merges, then the GPU **cross-encoder
  re-ranks (Stage 2)** to global top-k by `rerank_score`, then `compose_answer` over the
  root's `GraphStore`. No LLM synthesis; persistent cache TTL 3600 s.
- **`graph`**: per-project call-graph queries (definition/callers/callees/impact/…).
- **`overview`**: 15 `what=` views (structure, communities, status, projects, patterns,
  metrics, architecture_domains, hierarchy, import_cycles, surprising_connections,
  feature_map, business_rules, process_flows, suggested_questions, service_mesh).

## 9a. Reranking (Stage 2)

All MCP query paths run a **two-stage retrieval** pipeline (GPU; no CPU fallback):

- **AXIS A — code chunks**: vector retrieve (`sqlite-vec`, overfetch `top_k×3`), then
  cross-encoder rerank (`jinaai/jina-reranker-v1-turbo-en`) → sort by `rerank_score` →
  top_k. Federation: each member runs the above; union merged + re-sorted by `rerank_score`.
  Observability: `search()` records `rerank.queries` and `rerank.top1_changed` (the "lift"
  count where the cross-encoder moved a different chunk to position 1 vs the vector sort).
  Exposed via `GET /api/metrics` and `overview(what="metrics")`.
- **AXIS B — community/architecture context** (`scope="global"`, `_top_communities_semantic`):
  pool ≤50 community summaries per store, then cross-encoder rerank → sort by `rerank_score`
  → top_k. Replaced former bi-encoder cosine (`s_vecs @ q_vec`) approach.
- Rerank scores (jina logits) and vector scores are never blended across axes.
- Reranking runs **only** at query time; the index/KB-build pipeline never reranks.

## 9b. Inference lanes

| Lane | Surface | LLM(s) | Notes |
|------|---------|---------|-------|
| **A — MCP query** | `search`/`ask`/`graph`/`overview` via `/mcp` | embedding + reranking ONLY | No generation; delegated to the calling agent |
| **B — Dashboard chat** | `POST /api/chat_stream` | **claude-haiku-4-5** (Claude Code CLI) | Codex removed — haiku-only; no ollama, no deepseek |
| **D — KB summaries** | Background sweep (`enrich_community`/`_l2`/intents) | local ollama qwen3-enrich:1.7b via `chat(think=False)` | Write path only; `think=False` mandatory (no idle spin, #13461); unloads at idle |
| **E — KB classification + wiki narrative** | Background sweep (`classify_communities_semantic`; Phase B wiki) | cloud DeepSeek `deepseek-chat` (Ollama fallback) | Remote, bounded; key from env / `~/.bash_env`; never on the query path; provides accuracy the 1.7B model lacks |

The query generative LLM (**claude-haiku-4-5** via the Claude Code CLI) is reached **only** via the
dashboard chat box. Background KB enrichment uses **local ollama for summaries** (Lane D) and **cloud
DeepSeek for classification + Phase B wiki narrative** (Lane E) — both write-path only. MCP query
actions and `POST /api/ask` never generate text. **"GPU-only" (HR6) governs embeddings + reranking +
the local summary LLM**; the remote DeepSeek lane is not a CPU-fallback and never runs at query time.

## 16. Per-project config & federation inheritance

Each project may carry an optional `.opencode-index.yaml` (or `.yml`) at its root.
`core/index_config.py` governs config loading and resolution.

### 16.1 `ProjectConfig` fields

| Field | Default | Meaning |
|---|---|---|
| `index.exclude` | `[]` | Glob patterns; matched against file path relative to root |
| `index.use_default_ignores` | `true` | Apply `IGNORED_DIRS` (node_modules, .git, …) |
| `watcher.max_pending_files` | `10 000` | Watcher queue cap before forced flush |

### 16.2 `effective_config(path)` — inheritance model

`iter_files(root)` resolves config via `effective_config(root)` instead of loading
`.opencode-index.yaml` in isolation. Resolution rules:

1. **Standalone project** (no owning root in registry): `load_project_config(path)` — own file or defaults.
2. **Federation member** (path appears in some root's `federation` list):
   - `exclude` = **union** of root's globs + member's globs (order: root first).
   - `use_default_ignores`, `max_pending_files` = member's value when member has own config file, **else root's**.
3. Source label exposed in `overview(status).config.source`: `"own"` | `"inherited"` | `"default"`.

### 16.3 OSE config files are always indexed

`.opencode-index.yaml` and `.opencode-index.yml` **bypass** `exclude` patterns and
file-size limits in `iter_files()`. A user `exclude: ["*.yaml"]` rule never silently
drops the engine's own config from the index.

### 16.4 Config surfaced in `overview(status)`

`overview(what="status", project_path=…)` includes a `config` key:

```json
{
  "config": {
    "exclude": ["*.gen.py"],
    "use_default_ignores": true,
    "max_pending_files": 10000,
    "source": "inherited"
  }
}
```

`source` values: `"own"` (project has its own `.opencode-index.yaml`),
`"inherited"` (federation member using root's config), `"default"` (standalone, no config file).
