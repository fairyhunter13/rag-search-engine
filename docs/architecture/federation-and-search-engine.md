# Federation & Search-Engine Architecture — Part 1: Core

> Source-of-truth is `src/opencode_search/`. Last reconciled 2026-06-25. **2026-06-25**: Phase 1 — edge-free degeneracy exemption (D, HR20 `ec>0` guard on all three clauses); dashboard chat = Haiku-only (F, no DeepSeek fallback); architecture doc-sync + engineering principles register (§1a, HR27–HR29). **2026-06-23**: Phase 4A–F token-economy closes six LLM leaks (A=tail-classify guard `narrated=1`, B=batch L2 narration `enrich_communities_l2_batch`, C=full `llm_token_stats` instrumentation `classify/l2/bpre/l3.*`, D=narrated backfill `semantic_type IS NOT NULL`, E=batch BPRE narrative `_generate_narratives_batch` ≤20/call, F=L3 incremental self-heal via per-theme `child_sig` (Enzyme-IVM), 1800s window removed (`2380d45`)); cAST structural-path header prepended to every chunk (`chunk_file(project_root=...)`, arXiv 2506.15655); 3 stale Leiden refs corrected to k-core (HR24, shipped 2026-06-23). **2026-06-21**: H1–H3 universal symbol backbone (`tree-sitter-language-pack==1.9.1`, `process()` API, 306 langs, deleted `_TS_LANG`/`_DEF_KINDS`/`_CALL_NODE`/`_EXT_LANG`); G0–G5 5-tier resolution ladder (`kb/valueflow.py` Tier-1.5, `kb/resolve_rerank.py` Tier-1.75, `kb/llm_escalation.py` Tier-2, `deepseek-v4-flash` pin); HR15 Category B updated; HR16–HR19 added; §7a expanded. **2026-06-20**: BPRE Phase D + HR14; codex removed → haiku-only HR10; direct-DeepSeek classifier HR11; `think=False` HR12; regex→tree-sitter HR15.
> Continued in [federation-ops-and-invariants.md](federation-ops-and-invariants.md).

## 1. Purpose & scope

opencode-search is a local, GPU-only semantic code-search and KB engine. It indexes one or
more project trees and serves five MCP tools (`search`, `ask`, `graph`, `overview`,
`index`) plus an HTTP dashboard from a single daemon at `127.0.0.1:8765`.

**Federation** treats a *root* project that contains **symlinks to external sub-repos** as
one **logical repository**, while storing and indexing each linked sub-repo ("member") as
an independent unit.

## 1a. Engineering principles (doctrine)

The governing principle is **P0: most efficient + most effective, for *everything* in OSE**. Every component, lane, and algorithm is chosen and tuned for maximum efficiency *and* effectiveness; all principles below are corollaries (§9b per-workload engine assignment, no cross-lane bleed; HR6, HR9, HR10, HR12, HR26).

1. **DeepSeek = least/minimum token usage (DIKW token economy).** The cloud generative lane spends the *fewest possible* tokens: significance-gated head only, prefix-cached, abstention on the tail, child-reuse roll-ups; `$0` when quiescent; spend only to climb Information→Knowledge→Wisdom, and only at the nodes/queries actually read (HR22–HR24).
2. **tree-sitter, then LLM — no dynamic or static mapping, no keyword, no regex.** The only code that classifies *what the user's code means* uses tree-sitter (structural facts) first, then LLM (semantic/linkage facts) for what tree-sitter cannot statically reach. No regex, no static/dynamic keyword list, no mapping table may substitute for structural analysis of user code (HR14, HR15, HR16 5-tier ladder, HR17, HR18, §7a).
3. **GPU-only inference; CPU fallback fatal; maximize GPU, minimize CPU & RAM** (HR6, HR26, HR32, P16). Idle target: < 1 % CPU, constant RAM floor (models unload after `OPENCODE_MODEL_IDLE_UNLOAD_S`). The heavy KB cascade runs only on real source-fingerprint drift (`_last_enriched_sig` gate in `on_change`). File-watching is event-driven via watchdog/inotify (P17, HR33); poll fallback is last-resort only.
4. **No local generative LLM** — cloud DeepSeek for KB, Claude Code for chat/docgen (HR12).
5. **Determinism + idempotence** — byte-identical reruns with LLM off; enrichment gated on `summary IS NULL`, never re-labels settled rows (HR3, HR11, HR13, HR20, HR21, HR24, HR25).
6. **Federation = query-time union; MCP read-path is retrieval-only.** Every MCP action (`search`/`ask`/`graph`/`overview`) returns **root + all federated members combined** (query-time union; no cross-repo edges). The MCP query lane runs **no generative LLM inference** — only GPU **embedding** (+ cross-encoder rerank) for retrieval; all generative spend (DeepSeek) is **enrichment-time**, pre-built into the KB the read-path serves (HR4; §9b Lane A; read-only-MCP invariant). Federated readiness = **worst-of-members** (HR7); one absolute path = one index dir, per-project content-addressed stores (HR5).
7. **Self-healing** — event-driven (watcher) + reconcile re-derive on algo/source drift (HR1, HR2, HR25, §10).
8. **Root-only docgen + universal config** — one root-owned `docs/`; `.opencode-index.yaml` honored by every enumerator (HR27, HR28, HR29).
9. **Two-stage retrieval; rerank is the relevance authority.** Query = bi-encoder vector recall (`sqlite-vec`) → cross-encoder rerank (`jina-reranker-v1-turbo-en`, GPU); results ordered by `rerank_score`, **never the bare vector `score`**; **both** AXIS A (code chunks) and AXIS B (community/architecture context) are reranked; reranking runs **at query time only**, never at index/KB-build time (HR8; inv#10, inv#11).
10. **Public-repo hygiene.** Every emitted artifact (wiki `community_*.md`/`domain_*.md`, `federation.md`, BPMN, docgen pages, citations) is **project-root-relative**; absolute device paths and company/device names never leak — `symbols.file` stores absolute paths, so strip to root-relative before any artifact (HR13).
11. **Engineering doctrine** — every line of code is a liability (prefer no change → deletion → smallest sufficient diff); correctness before speed; live suite uses no mocks (real embedder + GPU). Machine-verified Concept→Spec→Impl→Test traceability closes the V&V loop (HR30).

## 1b. World model & governance/spec WM *(updated Phase 1 2026-06-26)*

OSE's world model is a **governance/spec WM** (see `docs/world-model/` + `docs/reference/world-model.md`):
- **State** = codebase + invariants/laws (P0–P15 in §1a + `model.yaml`)
- **Action** = a diff/change
- **Guard** = does the diff satisfy the preconditions?
- **Planner/validator** = `scripts/check_world_model.py` (GPU-free; emits CONFORMS/AT_RISK)

The old `kb/world_model.py` Requirements Traceability Matrix (`overview(what='world_model')`, HR30) was **deleted** (WS-B 2026-06-26) along with `FEATURES.md`. The governance/spec WM in `docs/world-model/model.yaml` (L1–L4 layers) **replaces** it as the normative source of truth. `scripts/check_world_model.py` provides the executable conformance check.

RTM: §1a principles → §13b HRs → §14 test map (three layers). L3 traceability is machine-verified by `test_world_model_traceability.py` (asserts every `model.yaml` L3_specs `test:` resolves to a live `def test_…`). `test_feature_proof.py` guards non-import of deleted modules.

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
- **Enrichment LLM**: cloud **DeepSeek** `deepseek-v4-flash` only (summaries, intents, semantic-type classification, Phase B wiki narrative) — **no local generative LLM**. Crashes if `DEEPSEEK_API_KEY` absent. The dashboard chat LLM (**claude-haiku-4-5** via Claude Code CLI only, **no DeepSeek fallback** — emits SSE error when CLI unavailable) is **dashboard-chat only** — never called by MCP tools. **Codex support removed.**

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
  | 2 | SEA-style LLM select (`kb/llm_escalation.py`) | 0.7 `_llm` | ON when `DEEPSEEK_API_KEY` present |
  | 3 | Whole-file LLM on parse-error files | 0.5 `_llm_file` | ON when `DEEPSEEK_API_KEY` present |
- Tier 2/3 are **always on by default**, suppressed only when `DEEPSEEK_API_KEY` is absent.
  **`OSE_DEEPSEEK_MODEL`**: override (default `deepseek-v4-flash`; `deepseek-chat` deprecates 2026-07-24).
  Without a key: reconstruction is GPU-free and byte-identical.
- **Guard test**: `test_no_code_semantic_regex.py` enforces the Category-A/B boundary;
  any new `re.compile`/`re.finditer` in Category-A paths fails CI. `test_valueflow_dynamic.py`,
  `test_rerank_resolution.py`, `test_llm_escalation_ladder.py`, `test_deterministic_resolution.py`
  prove the full ladder end-to-end (HR16–HR19 in Part 2).

## 8. Enrichment pipeline (`sweeps._enrich_project`)

1. Prune stale L1 communities.
2. Enrich **flat L1** communities with NULL summary (LLM; thermal guard at 80 °C).
   *(L2 `build_hierarchy` + L2 enrich steps deleted WS-B 2026-06-26 — flat-L1 only.)*
3. **Classify `semantic_type`** for new/unclassified L1 communities
   (`classify_communities_semantic`, `reclassify_all=False`).
4. `build_wiki(gs, wiki_dir)` — flat bundle: type-grouped `index.md`, deterministic
   `community_{id}.md` (reused summary + root-relative source citations + edge-drawn mermaid).
   No GPU; L1 narration is cloud DeepSeek. *(L2 `domain_{id}.md` pages deleted WS-B 2026-06-26.)*
5. `build_federated_index(project_path)` + regen of any owning root — writes the federated root's
   `federation.md` (aggregation of each member's own graph.db; no cross-repo edges, HR4). No-op
   for standalone projects. (See Part 2 §13b HR13.)
6. `reconstruct_processes(root_path)` (Phase D BPRE) — runs ONLY for federation roots (≥2 members).
   Writes `{index_dir}/process_graph.db`. **Three-tier extraction** (see §7a + **HR14/HR15** in Part 2):
   Tier 1 = tree-sitter (`kb/bpre_ast.py`, reusing `graph/extractor.py`): Pass A mines generated
   `*.pb.go` to discover the gRPC API surface (real constructor/registrar names, **no hardcoded
   patterns**); Pass B detects call sites per-file against that surface (gRPC, pub/sub, HTTP,
   status enums). Tier 2 = LLM linkage (ON when `DEEPSEEK_API_KEY` present) for config-driven
   edges (JSON-topic IDs, client hosts) tree-sitter cannot resolve. Tier 3 = cloud DeepSeek for D5
   rule text + D6 narrative. D4 process traces are **handler-anchored and deduped** — keyed on the
   entry handler's reachable symbol set, not any-edge service adjacency. **GPU-free** for Tier 1 +
   BPMN/mermaid; byte-identical without a DeepSeek key (F1/F2).
   (See §8b and **HR14** in Part 2.)

7. **`run_docgen` (root-only, manual-trigger only)** — generates the information hierarchy `docs/` tree at the federation root; pure members have their generated `docs/` cleaned instead (HR27). **NOT triggered by enrichment sweep** (removed Phase 2); CLI/dashboard only (`opencode-search docgen <project>`).
8. **`index_docs` (scope=docs)** — embeds generated `docs/` pages into the vector store; idempotent per-path replace; runs after `run_docgen`; watcher/fingerprint exclude `docs/` from the code path (HR28).

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
- **`overview`**: `what=` views: structure, communities, status, projects, patterns,
  metrics, import_cycles, surprising_connections, feature_map, business_rules, process_flows,
  suggested_questions, service_mesh, validate.
  *(`architecture_domains`, `hierarchy`, `world_model` deleted WS-B 2026-06-26.)*

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
| **B — Dashboard chat** | `POST /api/chat_stream` | **claude-haiku-4-5** only (Claude Code CLI); emits SSE error event when CLI unavailable | Codex removed; DeepSeek is KB-enrichment-exclusive (HR12) — no chat fallback |
| **D — KB enrichment** | Background sweep (`_enrich_project`: summaries/intents/classification/wiki) | cloud **DeepSeek** `deepseek-v4-flash` only | Write path only; `DEEPSEEK_API_KEY` required (crash-loud if absent); no local generative LLM |
| **E — Doc-tooling** | `opencode-search docgen/okf` (manual only; `POST /api/docgen`, `/api/okf`) | **`claude -p`** headless: **Haiku 4.5** (concept/write) + **Sonnet 4.6** (architect/discover) | LLM-native; no tree-sitter on doc path; no deterministic skeleton; kill-switches `OSE_DOCGEN=0`/`OSE_OKF=0`; never from auto-sweep or MCP (HR27/P13) |

**Four-lane invariant (HR12, HR31):**
- **LOCAL GPU lane** = embedding (FastEmbed/ONNX/CUDA, 768-dim) + cross-encoder reranking ONLY. CPU binding is fatal; any CPU fallback raises immediately.
- **CLOUD KB lane** = DeepSeek `deepseek-v4-flash` (KB enrichment: summaries, intents, semantic-type, wiki narrative; BPRE Tier-2 link resolution, Tier-3 parse-error, D5 rule text).
- **CLOUD chat lane** = claude-haiku-4-5 (dashboard chat only; no DeepSeek fallback).
- **DOC-TOOLING lane** = `claude -p` headless (docgen IH + OKF; Haiku/Sonnet; never KB, never chat).

**No local generative LLM exists in the engine.** Ollama/qwen3 were decommissioned 2026-06-20. MCP query actions (`search`/`ask`/`graph`/`overview`) perform embedding + reranking ONLY — no generation (HR9). In the 5-tier BPRE resolution ladder (HR16): Tier-1.75 is the GPU rerank lane; Tier-2/3 are cloud DeepSeek. Doc-tooling (`docgen`/`okf`) uses `claude -p` headless — a distinct lane from KB enrichment and chat (HR31).

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

### 16.5 Config honored by every enumerator (HR29)

`.opencode-index.yaml` is enforced not only in `iter_files` but in **every** project-source enumerator:

- **Incremental/watcher path** (`daemon/sweeps.py::on_change`): changed-file list filtered through `is_excluded(path, effective_config(project).exclude, project)` before embedding.
- *(Structural spine `kb/structure.py` deleted WS-B 2026-06-26.)*
- **BPRE walks** (`kb/bpre.py::_source_files`, `kb/bpre_ast.py::federation_discover`): per-member `is_excluded` filter applied; no-op when member has no config file.
- **Portal + docs walk** (Part A `repo_explore`, §8 step 8): inherit `effective_config` as above.

No enumerator may silently index a file that `effective_config` would exclude.
