# Federation & Search-Engine Architecture — Part 1: Core

> Source-of-truth is `src/opencode_search/`. Last reconciled 2026-06-23. **2026-06-23**: Phase 4A–F token-economy closes six LLM leaks (A=tail-classify guard `narrated=1`, B=batch L2 narration `enrich_communities_l2_batch`, C=full `llm_token_stats` instrumentation `classify/l2/bpre/l3.*`, D=narrated backfill `semantic_type IS NOT NULL`, E=batch BPRE narrative `_generate_narratives_batch` ≤20/call, F=L3 freshness 1800s guard in `build_federation_hierarchy`); cAST structural-path header prepended to every chunk (`chunk_file(project_root=...)`, arXiv 2506.15655); 3 stale Leiden refs corrected to k-core (HR24, shipped 2026-06-23). **2026-06-21**: H1–H3 universal symbol backbone (`tree-sitter-language-pack==1.9.1`, `process()` API, 306 langs, deleted `_TS_LANG`/`_DEF_KINDS`/`_CALL_NODE`/`_EXT_LANG`); G0–G5 5-tier resolution ladder (`kb/valueflow.py` Tier-1.5, `kb/resolve_rerank.py` Tier-1.75, `kb/llm_escalation.py` Tier-2, `deepseek-v4-flash` pin); HR15 Category B updated; HR16–HR19 added; §7a expanded. **2026-06-20**: BPRE Phase D + HR14; codex removed → haiku-only HR10; direct-DeepSeek classifier HR11; `think=False` HR12; regex→tree-sitter HR15.
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
- **Enrichment LLM**: cloud **DeepSeek** `deepseek-v4-flash` only (summaries, intents, semantic-type classification, Phase B wiki narrative) — **no local generative LLM**. Crashes if `DEEPSEEK_API_KEY` absent. The dashboard chat LLM (**claude-haiku-4-5** via Claude Code CLI, **DeepSeek fallback**) is **dashboard-chat only** — never called by MCP tools. **Codex support removed.**

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

1. **Chunk + embed** → `vectors.db` (`index/indexer.index_project`). Each chunk receives a deterministic structural-path header `# <repo-relative-path>` prepended to its content (cAST, arXiv 2506.15655; `chunk_file(project_root=...)`).
2. **Symbol extraction** (tree-sitter) over `iter_files(root, federation_mode=True)`.
3. **Call-edge resolution** (second pass): cross-file edges only.
4. **Community detection** (L1): fastgreedy modularity (`igraph.community_fastgreedy`, deterministic; edgeless symbols grouped by directory). Stamps `meta[algo_version]` + `meta[source_sig]` so `reconcile_projects` auto-repairs drift.
5. Stamp `indexed_at`, `file_count`, `chunk_count` in registry.

`federation_mode=True` prunes symlink dirs/files pointing **outside** the root — the
no-inlining invariant. Without it a root's file_count balloons ~12× by double-counting
linked trees.

## 7a. Code-semantic classification doctrine — tree-sitter + LLM only (HR15–HR19)

The *only* code that classifies *what the user's code means* uses **tree-sitter** (structural
facts) or **LLM** (semantic/linkage facts). No regex, no static/dynamic keyword list, no
mapping table may substitute for structural analysis of user code.

- **`kb/bpre_ast.py`** is the shared structural home for BPRE (§8 bullet 8) *and*
  `server/_overview.py _detect_services` (A2) — every module that needs to classify gRPC
  service structure delegates to this single tree-sitter scanner; no module holds its own regex.
- **Category B (exempt by name)** — updated 2026-06-21: (a) `graph/extractor.py` — generic
  call-node detector (node-kind ∋ `"call"`/`"invocation"`), `_MEMBER_KINDS`/`_BRANCH_NODE_KINDS`
  frozensets, `tree_sitter_language_pack.process()` + `StructureKind`/`SymbolKind` label-space.
  (`_TS_LANG`/`_DEF_KINDS`/`_CALL_NODE` **deleted** by H1–H3.) (b) `index/discover.py` —
  `detect_language_from_path` (replaces deleted `_EXT_LANG`/`detect_language`). (c) LLM output-
  vocabulary tables in `graph/enrich.py`/`kb/wiki.py`. (d) `server/_overview.py _VALID` API enum.
- **Universal 5-tier resolution ladder** (HR16–HR19, 2026-06-21): strictly monotone
  1.0→0.9→0.8→0.7→0.5; language-agnostic by construction; each tier sees only prior residue.
  | Tier | Mechanism | Conf | Gate |
  |---|---|---|---|
  | 1 | `process()` extraction — ANY language | 1.0 `EXTRACTED` | always |
  | 1.5 | value-flow/FQN-join (`kb/valueflow.py`) | 0.9 `RESOLVED` | always |
  | 1.75 | GPU cross-encoder rerank (`kb/resolve_rerank.py`) | 0.8 `RERANKED` | always |
  | 2 | SEA-style LLM select (`kb/llm_escalation.py`) | 0.7 `_llm` | `OSE_BPRE_LLM_LINK=1` |
  | 3 | Whole-file LLM on parse-error files | 0.5 `_llm_file` | `OSE_BPRE_LLM_FILE=1` |
- **`OSE_BPRE_LLM_LINK`** (default OFF): gates Tier-2. Off → tree-sitter-unreachable edges
  absent, never heuristically approximated. **`OSE_BPRE_LLM_FILE`** (default OFF): gates Tier-3.
  **`OSE_DEEPSEEK_MODEL`**: override (default `deepseek-v4-flash`; `deepseek-chat` deprecates 2026-07-24).
  With both OFF + `OSE_WIKI_LLM=0`: reconstruction is GPU-free and byte-identical.
- **Guard test**: `test_no_code_semantic_regex.py` enforces the Category-A/B boundary;
  any new `re.compile`/`re.finditer` in Category-A paths fails CI. `test_valueflow_dynamic.py`,
  `test_rerank_resolution.py`, `test_llm_escalation_ladder.py`, `test_deterministic_resolution.py`
  prove the full ladder end-to-end (HR16–HR19 in Part 2).

## 8. Enrichment pipeline (`sweeps._enrich_project`)

1. Prune stale L1 communities.
2. Enrich L1 communities with NULL summary (LLM; thermal guard at 80 °C).
3. If L2 absent **or over-granular** (`n_l2 > 2×round(√n_l1)` for n_l1≥4): `build_hierarchy` — two-phase L1-community-graph partition: Phase 1 = fastgreedy on connected L1 communities (≤round(√n_l1) groups); Phase 2 = isolated/edge-sparse L1 communities grouped by top-level directory. Edge-sparse repos (no cross-community edges) get a directory-based L2 instead of an empty hierarchy (M4).
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
   Writes `{index_dir}/process_graph.db`. **Three-tier extraction** (see §7a + **HR14/HR15** in Part 2):
   Tier 1 = tree-sitter (`kb/bpre_ast.py`, reusing `graph/extractor.py`): Pass A mines generated
   `*.pb.go` to discover the gRPC API surface (real constructor/registrar names, **no hardcoded
   patterns**); Pass B detects call sites per-file against that surface (gRPC, pub/sub, HTTP,
   status enums). Tier 2 = opt-in LLM linkage (`OSE_BPRE_LLM_LINK`, default OFF) for config-driven
   edges (JSON-topic IDs, client hosts) tree-sitter cannot resolve. Tier 3 = cloud DeepSeek for D5
   rule text + D6 narrative. D4 process traces are **handler-anchored and deduped** — keyed on the
   entry handler's reachable symbol set, not any-edge service adjacency. **GPU-free** for Tier 1 +
   BPMN/mermaid; byte-identical with `OSE_WIKI_LLM=0` and `OSE_BPRE_LLM_LINK` unset (F1/F2).
   (See §8b and **HR14** in Part 2.)

All enrichment is **idempotent and gated on `summary IS NULL`** (classification gated on
`semantic_type IS NULL OR non-canonical`), so the daemon never re-labels settled communities.

### 8a. LLM lanes within enrichment (resource-critical)

All KB enrichment — **summaries, symbol intents, semantic-type classification, wiki narrative** — runs
through cloud **DeepSeek** (`deepseek-v4-flash`) via `graph/llm.py:deepseek_chat`. **No local generative
LLM**; the daemon crashes loudly at `_enrich_project` entry if `DEEPSEEK_API_KEY` is absent
(`deepseek_key()` returns `None`). `temperature=0` keeps summaries and classification reproducible
(no churn). The DeepSeek classifier's output is final — HR15 (no-heuristic doctrine) governs;
no post-classification size or structural demotion. Embedding and reranking are unaffected by the
key — they bind ONNX/CUDA and run regardless.

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
| **B — Dashboard chat** | `POST /api/chat_stream` | **claude-haiku-4-5** primary (Claude Code CLI); **DeepSeek fallback** if haiku absent/empty | Codex removed; haiku insist — DeepSeek only on genuine haiku failure |
| **D — KB enrichment** | Background sweep (`_enrich_project`: summaries/intents/classification/wiki) | cloud **DeepSeek** `deepseek-v4-flash` only | Write path only; `DEEPSEEK_API_KEY` required (crash-loud if absent); no local generative LLM |

**Two-lane invariant (HR12):**
- **LOCAL GPU lane = embedding (FastEmbed/ONNX/CUDA, 768-dim) + cross-encoder reranking (`jinaai/jina-reranker-v1-turbo-en`) ONLY.** CPU binding is fatal; any CPU fallback raises immediately.
- **CLOUD generative lane** = DeepSeek `deepseek-v4-flash` (KB enrichment: summaries, symbol intents, semantic-type classification, wiki-L2 narrative; BPRE Tier-2 link resolution, Tier-3 parse-error files, D5 rule/state-machine text; dashboard-chat fallback) **+** claude-haiku-4-5 via the Claude Code CLI (dashboard-chat primary).

**No local generative LLM exists in the engine.** Ollama/qwen3 were decommissioned 2026-06-20. In the 5-tier BPRE resolution ladder (HR16): Tier-1.75 (`kb/resolve_rerank.py`) is the GPU rerank lane — embedding + reranking, zero generation; Tier-2/3 are cloud DeepSeek. MCP query actions (`search`/`ask`/`graph`/`overview`) perform embedding + reranking ONLY — no generation (HR9).

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
