# Federation & Search-Engine Architecture — Part 2: Ops, Transport & Invariants

> Continued from [federation-and-search-engine.md](federation-and-search-engine.md).

## 10. Event-driven lifecycle

- **Watcher** (`daemon/watcher.py`): inotify (watchdog) primary, 2 s burst suppression,
  `is_ignored_path` filter; poll fallback at 5 s (mtime snapshot diff). Watches **all
  enabled projects** — members included — because `register_all_members()` runs before
  `start_watcher()`.
- **`on_change(path, files)`** (`sweeps`): incremental `_index_files` on changed files (or
  full `_index_project` if `files` is empty), then **45 s-debounced** `_enrich_project`.
- **`reconcile_projects()`**: startup + `index()`-triggered one-shot. Calls
  `register_all_members()`, then for every enabled project with no chunks or zero
  communities (stalled pipeline): `_index_project` + `_enrich_project`. Globally pausable
  via `_PAUSED` (tests set this via the `pause_sweeps` autouse fixture).
- **`index(path, enabled=True)`** (MCP): rejects forbidden roots (`is_forbidden_root` →
  `/tmp`, `~/.cache`), upserts enabled, spawns a `reconcile_projects` thread. Registering a
  root therefore automatically discovers, indexes, enriches, and starts watching its members.

## 11. Removal & consistency

- **`index(path, enabled=False)`** (MCP): `expand_federation(path)` → `remove_project` +
  `rmtree(index_dir)` for each path. Removing a root cascades to its members; response
  reports `members_removed`.
- **Orphan vacuum** (§6 of part 1) is the backstop that reconciles storage to the registry
  if anything is left behind.

## 12. MCP transport architecture

Two transports serve the same 5 tools:

- **HTTP** — `mcp.streamable_http_app()` at `:8765/mcp`. One shared daemon, one model
  copy, no per-session process. **Preferred transport** (commit c48ba25).
- **stdio bridge** — `daemon bridge-stdio`: full in-process engine per client session
  (~1 GB). Retained as fallback; idle self-exit after `OPENCODE_BRIDGE_IDLE_S` (default
  600 s).

### 12.1 Config source-of-truth

`scripts/integrations/canonical.py` + `scripts/configure_integrations.py` write MCP
entries into 7 client configs. Canonical URL: `http://127.0.0.1:8765/mcp`.

| Client family | Format |
|---|---|
| Claude `settings.json` | `{"type":"http","url":"http://127.0.0.1:8765/mcp"}` |
| hermes `config.yaml` | `url: http://127.0.0.1:8765/mcp` (drop command/args/env) |
| opencode `opencode.jsonc` | `{"type":"remote","url":"http://127.0.0.1:8765/mcp"}` |

Dropping `env` is safe: `OPENCODE_ALLOW_INDEX_OUTSIDE_CWD` is unreferenced in `src/`;
LLM vars match daemon defaults; query-LLM is never called by MCP tools.

## 13. Invariants the engine MUST uphold

## 13a. Hard pipeline requirements — the contract

The diagram and HR table (§13b) are the normative write-path spec. A change
that violates them is an architecture regression; §14 maps each HR to the live
test that proves it.

```
┌──────────── QUERY PATH (synchronous, read) ────────────┐
│ MCP tools: search·ask·graph·overview·index (HTTP :8765) │
│ routes → mcp.py / _overview.py / routes_search.py       │
│ FEDERATION FAN-OUT  federation.py                        │
│   expand_federation(root) = [root] + symlink members     │
│   federated_map(fn) → fn on each member's OWN stores     │
│ union lists · worst-of kb_state · per-member stores      │
└────────────────────────────────────────────────────────┘

┌──────────── BACKGROUND PATH (async, write) ────────────┐
│ _start_background: scheduler(6h/idle/watchdog)           │
│   NO kb_sweep, NO periodic reconcile                     │
│ reconcile_projects() — once at startup (thread)          │
│ start_watcher() — inotify, all enabled projects, 2s      │
│   on_change(project, files)  ← ONLY steady-state trigger │
│     ├─ _index_files()   → incremental VECTOR re-embed    │
│     └─ _enrich_project() → KB build, 45s/project debounce│
│ _index_project = chunk+embed → symbols → edges → L1      │
│ _enrich_project = enrich L1+L2 (cloud DeepSeek)          │
│   → classify semantic_type (cloud DeepSeek)              │
│   → build_wiki  (deterministic pages + reused summaries; │
│       L2-domain narrative = cloud DeepSeek; NO GPU)      │
│   → build_federated_index (root federation.md; HR4 held) │
└────────────────────────────────────────────────────────┘
kb_state: indexing → searchable → enriching → ready
          (no vec)   (vec,l1=0)  (0<pct<95)  (l1≥95,l2=100)
```

## 13b. Hard requirements (HR)

| # | Hard requirement |
|---|---|
| **HR1** | Watcher is the steady-state indexing trigger; `on_change` incremental-embeds changed files. Full `_index_project` (graph+k-core) runs only at first index / reconcile. |
| **HR2** | KB enrichment is triggered by the same watcher event on its own 45s/project debounce. Event-driven only — no `kb_sweep` / periodic reconcile timer. |
| **HR3** | Re-running `detect_communities` / `_enrich_project` MUST NOT wipe existing summaries. `ready` stays `ready` across re-index (`summary=None` in `upsert_community`, never `""`). |
| **HR4** | Federation = query-time union; each member has its own stores; no cross-repo edges. Fan-out via `expand_federation`/`federated_map`. (≡ inv#1–#4.) |
| **HR5** | One absolute path = one index dir. Two distinct clones → two indexes. (≡ inv#2, #8.) |
| **HR6** | GPU-only for **embeddings + reranking** (FastEmbed/ONNX/CUDA); any CPU fallback raises fatally. No local generative LLM — the GPU lane is embeddings + reranking ONLY. (≡ inv#5.) |
| **HR7** | `kb_state` lifecycle: `indexing → searchable → enriching → ready`; `ready` iff `enriched_pct ≥ 95 AND l2_enriched_pct == 100`. Federated entity = worst-of members. |
| **HR8** | Two-stage retrieval at query time: Stage 1 = bi-encoder vector search (`sqlite-vec`); Stage 2 = cross-encoder rerank (`jina-reranker-v1-turbo-en`, GPU). Both AXIS A (code chunks) and AXIS B (community summaries) are reranked. Results ordered by `rerank_score`, never the bare vector `score`. Reranking never runs at index/KB-build time. |
| **HR9** | MCP query actions (`search`/`ask`/`graph`/`overview`) perform ONLY embedding + reranking — no generative LLM (cloud or local). `ask` → `compose_answer` = assembled context; generation is delegated to the calling agent (June 2026 MCP best practice). |
| **HR10** | Dashboard chat (`POST /api/chat_stream`) uses **claude-haiku-4-5** as primary (Claude Code CLI, `claude -p --model`); **DeepSeek fallback** when haiku CLI absent or returns empty output. **Codex support removed**. Dashboard-only: not wired to any MCP tool. |
| **HR11** | `semantic_type` is assigned by **direct LLM classification (cloud DeepSeek `deepseek-v4-flash`)** over title+summary (batch-20) — **no local fallback**. The daemon classifies only new/unclassified communities (`reclassify_all=False`) → never re-labels settled ones (no churn). The LLM classifier's output is final — HR15 (no-heuristic doctrine) governs; no post-classification size demotion. No embeddings/prototypes in classification. |
| **HR12** | **No local generative LLM** — KB build (`_enrich_project`) uses **cloud DeepSeek only** for all generative tasks (summaries, intents, classification, wiki narrative). The daemon crashes loudly at `_enrich_project` entry if `DEEPSEEK_API_KEY` is absent (`deepseek_key()` returns `None`). GPU lane is embeddings + reranking ONLY. (Supersedes the retired local-LLM idle-spin invariant.) |
| **HR13** | The **wiki is a per-member artifact** built by `build_wiki(store, dir)`: `index.md` (type-grouped ToC), one deterministic `community_{id}.md` per L1 (reused DeepSeek summary as prose, member table with **project-root-relative** source citations, call-graph mermaid drawn from real `edges`), and one `domain_{id}.md` per L2 (DeepSeek narrative, templated fallback). Community pages and diagrams use **no LLM** (deterministic → byte-identical reruns with `OSE_WIKI_LLM=0`); only L2 narrative calls **cloud DeepSeek** — no embedder, no local server. A federated root additionally gets `federation.md` via `build_federated_index` — **presentation-only** aggregation of each member's own graph.db (domains, key business communities, semantic_type rollup); it creates/reads **no cross-repo edges** (HR4 preserved). Citations are root-relative so the absolute device path never leaks (public repo). |
| **HR14** | **BPRE process graph is a root-level artifact**: `process_graph.db` lives in `root_process_db(root_path)` (the federation root's index dir), never in any per-member `graph.db` (HR4 preserved). `reconstruct_processes(root_path)` runs only for roots with ≥2 members. **Three-tier extraction model** — **Tier 1 (tree-sitter, `kb/bpre_ast.py`)**: two-pass structural scan reusing `graph/extractor.py` — Pass A discovers the gRPC/proto API surface by mining generated `*.pb.go` (real constructor + registrar names; no hardcoded patterns, no regex); Pass B detects call sites per-file against that surface (gRPC clients/servers, proto imports, pub/sub publish/consume, HTTP routes/clients, status enums). **Tier 2 (opt-in LLM linkage, `OSE_BPRE_LLM_LINK`, default OFF)**: when enabled, DeepSeek resolves config-driven links (JSON-topic IDs, client hosts) tree-sitter cannot statically reach; emitted with `confidence < 1.0` + `_llm` kind suffix; flag OFF ⇒ tree-sitter-unreachable edges are **absent**, never heuristically approximated. **Tier 3 (cloud DeepSeek)**: D5 rule-text extraction + D6 process narrative (unchanged). D4 process traces are **handler-anchored and deduped**: each process is keyed on an entry handler's reachable symbol set (BFS over intra-service call graph from the handler), not any-edge service adjacency — no duplicate chains, no spurious transitivity, no test-file entry points. The Tier-1 pass is **GPU-free** and byte-identical on repeated runs (`OSE_WIKI_LLM=0`; `OSE_BPRE_LLM_LINK` unset ⇒ F1/F2 preserved). Surfaces: `overview(what='process_flows')` returns reconstructed processes when db exists; `GET /api/process/bpmn` exports XSD-valid XML; the dashboard Processes view renders sequenceDiagrams via Mermaid. |
| **HR15** | **Engine-wide no-heuristic doctrine (tree-sitter + LLM only)**: the *only* code that classifies *what the user's code means* uses **tree-sitter** (structural facts) or **LLM** (semantic/linkage facts). No regex, no static/dynamic keyword list, no mapping table may substitute for structural analysis of user code. **Category A — eliminated** (all three sites migrated as of 2026-06-20): A1 `kb/bpre.py` 12 `re.compile` structural patterns → `kb/bpre_ast.py` Tier-1 tree-sitter; A2 `server/_overview.py _detect_services` duplicate gRPC regex → `bpre_ast` Pass-A discovery (single source of truth shared with BPRE); A3 `kb/patterns.py _KNOWN` static framework map → real `graph/llm.deepseek_chat` call (cached per dep-set, raw-dep-name fallback when no key). **Category B — intrinsic mechanism, exempt by name** (updated 2026-06-21 after H1–H3 universal symbol backbone): (a) `graph/extractor.py` — generic call-node detector (node-kind ∋ `"call"`/`"invocation"`), surviving `_MEMBER_KINDS`/`_BRANCH_NODE_KINDS` frozensets, and `tree_sitter_language_pack.process()` + `StructureKind`/`SymbolKind` label-space — tree-sitter's own grammar vocabulary. **Note**: `_TS_LANG`/`_DEF_KINDS`/`_CALL_NODE` **deleted** by H1–H3 (universal `process()` API replaces per-language dicts); (b) `index/discover.py` — `detect_language_from_path` (replaces deleted `_EXT_LANG`/`detect_language` per-extension table), required before any tree-sitter parse; (c) `graph/enrich.py` + `kb/wiki.py` type-order/label tables — the LLM classifier's output vocabulary contract; (d) `server/_overview.py _VALID` + SQL semantic-type filters — API surface enum + LLM-output consumers, not pre-LLM classifiers. **Guard test**: `test_no_code_semantic_regex.py` — `test_no_code_semantic_regex_in_category_a` fails if any `re.compile`/`re.finditer` appears in Category-A paths; `test_category_b_allowlist_is_exhaustive` fails if regex appears outside the explicit allowlist (anti-erosion lock). |
| **HR16** | **Universal 5-tier resolution ladder** (2026-06-21, `84aa9cf`): language-agnostic by construction, strictly monotone confidence 1.0→0.9→0.8→0.7→0.5; each tier sees only the prior tier's residue; the paid LLM only selects from symbolically-admitted candidates. **Tier 1** (`graph/extractor.py`): pack-native `process()` — ANY language; generic call-node detection; conf 1.0 `EXTRACTED`. **Tier 1.5** (`kb/valueflow.py` + `kb/bpre.py`): deterministic intra-procedural value-flow/constant-propagation (non-literal keys: const/var/:=/`Sprintf`→value; insert→dispatch into maps/DI/topics) + IDL/proto FQN cross-language join + generic cascade; conf 0.9 `RESOLVED`; residue emitted not dropped; no regex, no framework vocab. **Tier 1.75** (`kb/resolve_rerank.py`): GPU-local embed→cross-encoder rerank using structural/type context — free (model already warm), ~60× cheaper than LLM localization; single candidate always binds; conf 0.8 `RERANKED`; gate: OSE default. **Tier 2** (`kb/llm_escalation.py` + `kb/bpre.py`): SEA-style LLM — selects from the symbolically-admitted candidate set, never authors; callee verified ∈ index; no self-consistency, no strict-schema; conf 0.7 `_llm`; gate: `OSE_BPRE_LLM_LINK=1`. **Tier 3** (`graph/extractor.py` + `kb/bpre.py`): whole-file LLM on parse-error/empty-structure files only; conf 0.5 `_llm_file`; gate: `OSE_BPRE_LLM_FILE=1`. **Invariants**: zero per-language/per-framework vocab and no `import re` in the resolution path (generic AST node-kind primitives + FQN + embeddings only); with both gates OFF + `OSE_WIKI_LLM=0`, reconstruction is GPU-free, byte-identical, and deterministic. **Pack facts**: `tree-sitter-language-pack==1.9.1` / `tree-sitter==0.25.2`, 306 canonical + alias ≈ 312 langs, typed `process()`, broad-but-not-universal structure coverage. |
| **HR17** | **Deterministic Tier-1.5: intra-procedural value-flow + FQN join** (`kb/valueflow.py`, 2026-06-21). Handles the *dynamic-mapping* case: non-literal call arguments (const/var assignment, `:=` short-var-decl, `Sprintf`, field lookup) are resolved through per-file `{identifier → string_value}` def-use maps built by a single AST pass. Language coverage: Go (`const_spec`/`var_spec`/`short_var_declaration`), Python (`assignment`), JS/TS (`variable_declarator`), Java/Kotlin (`local_variable_declaration`/`property_declaration`). `resolve_first_arg` follows literal fast-path → def-use identifier → selector field; returns `None` for true dynamics (call-result, reflection) → falls to GPU-rank. FQN join: cross-language edge resolved by IDL/proto fully-qualified name (conf 0.9 `RESOLVED`). Residue is emitted as candidates (not dropped) for Tier-1.75 disambiguation. **Strictly no regex, no framework vocab**: structural AST-node-kind primitives only. Feasibility: YASA (arXiv:2601.17390) build-free data-flow at 31.8 KLOC/min. |
| **HR18** | **GPU Tier-1.75 + SEA-style verified LLM + V4 Flash** (`kb/resolve_rerank.py` + `kb/llm_escalation.py` + `graph/llm.py`, 2026-06-21). Tier-1.75: `rerank_candidates(query_context, candidates, *, margin=0.05)` — GPU cross-encoder (already warm); single candidate always binds; multi-candidate binds when gap ≥ margin; falls through to Tier-2 when gap < margin. `rerank_residue` applies Tier-1.75 across residue items; resolved carry conf=0.8. SweRank (arXiv:2505.07849): retrieve-and-rerank beats Claude-3.5 localization at ~60× lower cost; SACL (arXiv:2506.20081): structural/type context (not bare names) improves reranker precision. Tier-2 (`escalate`): SEA-style (arXiv:2408.04344) — LLM selects from the admitted set, never authors; callee verified ∈ index; `stable_prefix` cache (DeepSeek 384K context, `json_object` non-thinking); no self-consistency, no strict-schema enforcement. Model: `deepseek-v4-flash` (pinned; `deepseek-chat` alias deprecates 2026-07-24); override via `OSE_DEEPSEEK_MODEL`. Cache stats exposed at `/api/metrics` as `llm_cache.{hits,misses,calls}`. |
| **HR19** | **DEFERRED — cross-service recall complement** (documented, not built). Static IDL/FQN-join = static SOTA with a finite recall floor. Next-phase lift: (a) test-time OpenTelemetry traces via servicegraph connector — confirms dynamic paths at ~0.95; (b) gRPC reflection at runtime — resolves service-to-endpoint bindings without generated code. Reconciliation: trace-confirmed-static / trace-only (dynamic-flag) / static-only. Anti-hallucination guard (arXiv:2512.12117): structural verification gate stays mandatory even with trace data. |
| **HR20** | **Composite partition-quality gate on `kb_state`** (`graph/quality.py`, 2026-06-22). `partition_quality(store)` is deterministic (igraph + SQL, zero LLM) and computes a composite score: `modularity_q` (igraph.modularity), `coverage` (intra-community edges / total), `singleton_ratio` (L1 with member_count==1 / n_L1). `degenerate=True` when `singleton_ratio ≥ 0.60 OR (edges>0 AND coverage < 0.20) OR (edges>0 AND n_l1≥2 AND modularity_q < 0.05)`. A degenerate partition demotes `kb_state` from `'ready'` to `'searchable'` in `overview(status)`. **Modularity Q alone is explicitly rejected** as the sole gate (exponentially many near-optimal partitions, degrades on sparse code graphs; CPM preferred for code — arXiv 2501.07025). Edge-free projects (file-level fallback) are exempt from coverage/modularity checks. Federation roots with synthesis L3 communities (0 call edges) are exempt from `symbol_hollow`. |
| **HR21** | **Federation-global L3 roll-up synthesis** (`kb/federation_hierarchy.py`, 2026-06-22). `build_federation_hierarchy(root_path)` synthesises ≤8 cross-service domain themes as `level=3` rows in the root `graph.db`. Input = already-enriched per-member **L2 community summaries only** (zero per-symbol or L1 re-reads → near-zero token cost; arXiv 2606.02019 composition invariants + GraphRAG roll-up: child-reuse costs 97% fewer tokens than source). Themes grouped deterministically by `semantic_type`; each gets one DeepSeek roll-up synthesis call (≤8 per root per enrich) honoring `OSE_WIKI_LLM=0`/missing-key → deterministic templated fallback (byte-identical). L3 rows carry `semantic_type='domain'` so `ask(scope=global)` / `_macro_community_context` picks them up automatically (they pass the `NOT IN ('test','tooling')` filter). **HR4-safe**: `build_federation_hierarchy` creates no edges. Called in `_enrich_project` after `build_federated_index`. **Member-edit refresh**: `_regen_owning_hierarchy(member_path)` in `sweeps.py` (2026-06-22) ensures that when any member is enriched its owning roots' L3 is also rebuilt — mirrors `_regen_owning_federations` for `federation.md`. **1800s freshness guard** in `build_federation_hierarchy` (2026-06-23): skips rebuild when root `graph.db` was written within the last 1800 s and L3 rows already exist — caps a 25-member edit burst at ≤1 L3 rebuild per 30 min per root (mirrors `reconstruct_processes`'s reuse guard). **Recursive multi-level Leiden is explicitly rejected** (exponentially-many near-optimal partitions on sparse graphs; future deepening must use k-core decomposition — arXiv 2603.05207). |
| **HR22** | **Deterministic structural spine** (`kb/structure.py`, 2026-06-23). `build_structure_tree(store, root)` inserts `level=0, kind IN ('dir','file')` nodes into the `communities` table — the repo→dir→file hierarchy as the primary Information-rung representation (zero LLM tokens, byte-reproducible). CRC32-based ids (dir `+2^30`, file `+2^31`) prevent collision with semantic L1/L2 communities. `member_count` = direct-child count (dirs) or symbol count (files). **`semantic_type` is always NULL for structural spine rows** — structural paths are never LLM-typed; they are not semantic communities. `narrated` is always 0 for structural spine rows — the Wisdom rung never applies to directory/file nodes. `community_count()` scopes `level>=1`, excluding spine rows. All semantic retrieval selectors (`_top_communities_semantic`, `_community_context`, `_tree_walk_context`) carry `kind NOT IN ('dir','file')` so spine never leaks into query context. The dashboard Hierarchy view (`overview(what='hierarchy')`) is the only surface that explicitly renders both level=0 (structural) and level≥1 (semantic) nodes. |
| **HR23** | **DIKW token economy** (`graph/enrich.py`, 2026-06-23). Spend LLM tokens only to climb Information→Knowledge→Wisdom, and only at the nodes/queries actually read. **Information** (level=0 structural spine + L1 deterministic k-core community structure + structural titles/summaries via `label_community_structural`) = 0 tokens, byte-reproducible. **Knowledge** (LLM on the significance-gated head only): `compute_significance()` classifies all unenriched L1 into head (`member_count≥8 OR ≥2 cross-community edges`) and tail; head is enriched in batches of 20 via `deepseek_extract(stable_prefix, dynamic_tail)` (prefix-cached), capped by `OSE_ENRICH_BUDGET_TOKENS`. `_enrich_one_batch` sets `narrated=1` on every successfully enriched community (idempotency guard). **Abstention on unrecognised types**: when DeepSeek returns a type not in `_TYPE_ORDER`, `semantic_type` is set to `NULL` (reject-option doctrine, NCwR arXiv 2412.03190) — never forced to `"utility"`. **Tail abstains**: `label_community_structural` sets `semantic_type=NULL, narrated=0` — the Wisdom path narrates on demand. **`narrated` column** (`communities.narrated INTEGER DEFAULT 0`): 0 = unnarrated (tail, structure, or not-yet-lazily-narrated); 1 = LLM-narrated (head or lazily-narrated tail). All semantic retrieval selectors filter `narrated=1` — un-narrated tail is invisible to retrieval until lazily promoted. `llm_token_stats()` (flat dotted-namespace keys) exposes the full token budget at `/api/metrics` and `overview(what='metrics')`. Namespaces (2026-06-23): `enrich.*` (L1 head batched narration), `classify.*` (semantic-type classification, gated `narrated=1` — zero calls for abstained tail), `l2.*` (L2 batch narration via `enrich_communities_l2_batch`), `l3.*` (federation L3 roll-up), `bpre.*` (BPRE process narrative batched). Each namespace carries `.calls`, `.completion_tokens`, `.prompt_cache_hit_tokens`, `.prompt_cache_miss_tokens`. `classify.calls==0` invariant: the tail abstains, so classify only fires on narrated head rows. |
| **HR24** | **Deterministic hierarchy: fastgreedy modularity + two-phase L2 partition** (`graph/community.py`, `kb/hierarchy.py`, 2026-06-23). **L1**: `detect_communities` uses `igraph.community_fastgreedy().as_clustering()` (Clauset-Newman-Moore, agglomerative-greedy) on the symbol call-graph's undirected edged subgraph; edgeless symbols grouped by directory (avoids N-singleton explosion). RNG seeded at module import for byte-identical runs (`random.seed(0)` + `igraph.set_random_number_generator`). Replaces the old exact-k-shell partition which fragmented connected nodes across shell boundaries into singletons (k-core is a node ranking, not a partition). **L2** (`build_hierarchy`): two-phase L1-community-graph partition. Phase 1: fastgreedy on connected L1 communities → ≤round(√n_l1) groups. Phase 2: isolated L1 communities (no cross-community call edges) → group by top-level directory relative to project root. Edge-sparse repos (no cross-community edges at all) fall through entirely to Phase 2, producing a directory-based L2 instead of an empty hierarchy (M4 fix). Version constants: `ALGO_VERSION` in `community.py`, `HIER_VERSION` in `hierarchy.py`. **`leidenalg` must not be imported** in `community.py` or `hierarchy.py` — SC8a guard enforces this. **SC8b guard**: `detect_communities` is byte-identical on two independent runs from the same graph. **Adaptive reasoning-retrieval** (`query/ask.py`, 2026-06-23): `_tree_walk_context()` drills `parent_id` from L2 domains → L1 children, reranks by summary relevance (GPU cross-encoder), returns traceable `[Domain → Community]` context. Used for `global` and `architecture` scopes instead of the old flat community pool. Falls back to flat L1 pool when no L2 hierarchy exists yet. **Wiki derived view** (`kb/wiki.py`, 2026-06-23): `_render_domain` reuses the stored L2 summary (from `enrich_community_l2`) instead of calling `_narrate()` — zero extra LLM tokens when summary is available. |
| **HR25** | **Self-healing graph pipeline** (`graph/store.py` meta table; `daemon/sweeps.py` M1-M4, 2026-06-23). **M1 — version + source stamps**: `graph.db` carries a `meta(key,value)` table (survives `GraphStore.clear()`). Two keys: `algo_version = f"{ALGO_VERSION}+{HIER_VERSION}"` (bumped by editing either constant) and `source_sig` (SHA-1 of sorted `relpath:mtime` for `iter_files(root, federation_mode=True)` — stat-only, GPU-free). Both stamped by `_index_project` after `detect_communities` and by `_rederive_graph` after re-derive. **M2 — reconcile self-heals stale graphs**: `reconcile_projects` now calls `_graph_stale(path, gs)` in the non-federation branch; when `True`, calls `_rederive_graph(project_path)` + `_enrich_project`. `_rederive_graph` is GPU-free (tree-sitter + igraph): clears graph, re-extracts symbols+edges via `_extract_graph(gs, root)`, re-detects communities, wipes stale L2+, restamps both meta keys. Rides the existing 30-min `_reconcile_loop` — no new timers. Federation roots keep their thin own-graph (skipped by the `not entry.federation` guard); their L3 heals transitively via `_regen_owning_hierarchy` when members heal. **M3 — enrich-time L2 coarseness guard**: `_enrich_project` rebuilds L2 when `n_l2 == 0` or (`n_l1 ≥ 4` and `n_l2 > 2×round(√n_l1)`) — heals over-granular hierarchies on every enrich path (reconcile, `on_change`, burst-enrich). **M4 — edge-sparse completeness**: `build_hierarchy` no longer returns `0` on projects with no cross-community call edges; instead falls through to the Phase-2 directory-group path, guaranteeing every project with ≥2 L1 communities gets at least one L2 domain. **GPU-only invariant preserved**: `_rederive_graph` never calls `get_embedder` or `embed()`. |
| **HR26** | **GPU execution provider auto-detect** (`core/gpu.py`, 2026-06-23). `_GPU_EP_ORDER` whitelist (CPU excluded): `NvTensorRTRTXExecutionProvider → TensorrtExecutionProvider → CUDAExecutionProvider → MIGraphXExecutionProvider → ROCMExecutionProvider → DmlExecutionProvider`. `rank_gpu_providers(available, *, disable_tensorrt)` is pure (deterministic, no GPU). `select_gpu_device()` (@lru_cache): sets `CUDA_DEVICE_ORDER=PCI_BUS_ID` before CUDA init, enumerates via `pynvml` (max free VRAM → tie-break compute capability → lowest index), honours `OPENCODE_GPU_DEVICE`. `select_gpu_providers()`: preloads ORT NVIDIA DLLs, ranks real `ort.get_available_providers()`, **raises `RuntimeError` if empty** (CPU forbidden — fatal). Both `Embedder` and `Reranker` call `assert_gpu_available()` in `__init__` and bind via `select_gpu_providers()` in `_init()`; session `add_session_config_entry("session.disable_cpu_ep_fallback", "1")` prevents ORT silent CPU binding; binding guard asserts `session.get_providers()[0] ∈ GPU_EP_NAMES`. `assert_gpu_available` / `is_gpu_available` are the renamed backward-compat aliases. |

1. **No inlining** — external symlinked sub-repos are never indexed into the root
   (`federation_mode=True`); indexed only as independent members.
2. **Members are first-class** — every member is an enabled, separately-searchable project
   with its own DBs.
3. **`root.federation` is authoritative** and re-synced on every `index_members` call.
4. **Logical-repo coverage** — `search(project_paths=[root])` and `ask(project_path=root)`
   expand through `expand_federation` to cover root + all members.
5. **GPU-only** — embeddings and enrichment run on CUDA; CPU fallback aborts the daemon.
6. **Forbidden roots** (`/tmp`, `~/.cache`) are never registered.
7. **Idempotency** — discovery, registration, reconcile, enrichment, and config repair all
   converge on reruns.
8. **Registry↔storage consistency** — cascade-remove + orphan-vacuum keep `projects.json`
   and `INDEX_ROOT` in agreement.
9. **MCP query actions never generate** — the query generative LLM (**claude-haiku-4-5** + DeepSeek fallback) is
   reached only via the dashboard chat box (`POST /api/chat_stream`); MCP query actions
   (`search`/`ask`/`graph`/`overview`) and `compose_answer` never generate text. Build-path
   generation is confined to background KB enrichment: **cloud DeepSeek only** — summaries,
   intents, classification, and L2-domain wiki narrative. Wiki community pages and all
   diagrams/citations are **deterministic** (no LLM). No local generative LLM. (≡ HR9, HR12, HR13.)
10. **Reranking is the relevance authority** — every query result set is ordered by
    `rerank_score`, never the bare vector `score`; cross-encoder runs at query time only. (≡ HR8.)
11. **Both retrieval axes are cross-encoder-ranked** — AXIS A (code chunks) and AXIS B
    (community/architecture context) both pass through `jina-reranker-v1-turbo-en` at
    query time before the context is assembled. (≡ HR8.)
12. **Dashboard chat uses claude-haiku-4-5 primary** via the Claude Code CLI; **DeepSeek fallback** on failure; **codex removed**. (≡ HR10.)

## 14. Test coverage map

Each §13 invariant has a corresponding live test that proves it without mocks:

| Invariant | Test | File |
|---|---|---|
| #1 no-inlining | `test_inv1_no_inlining` | `test_federation_architecture.py` |
| #2 members first-class | `test_inv2_members_first_class` | `test_federation_architecture.py` |
| #3 federation authoritative | `test_inv3_federation_authoritative` | `test_federation_architecture.py` |
| #4 logical-repo coverage | `test_inv4_root_scoped_search_fanout` | `test_federation_logical_entity.py` |
| #6 forbidden root | `test_inv6_forbidden_root` + `test_upsert_project_rejects_forbidden_root` | `test_federation_architecture.py` / `test_p22_kb_e2e.py` |
| #7 idempotency | `test_index_project_idempotent` | `test_p22_kb_e2e.py` |
| #8 cascade remove | `test_inv8_cascade_remove` | `test_federation_architecture.py` |
| HR1 watcher→index | `test_p34_watcher_updates_vector_index` | `test_p6_daemon.py` |
| HR2 watcher→KB / event-driven | `test_watcher_kb_e2e` | `test_p6_daemon.py` |
| HR3 enrichment idempotence | `test_detect_communities_idempotent` | `test_p3_graph.py` |
| HR4 federation fan-out | `test_real_federation_fanout` + `test_inv4_root_scoped_search_fanout` | `test_p22_kb_e2e.py` / `test_federation_logical_entity.py` |
| HR5 one path → one index | `test_inv2_members_first_class` + `test_inv8_cascade_remove` | `test_federation_architecture.py` |
| HR6 GPU-only | `test_no_cpu_fallback`, `test_embedder_bound_to_cuda` | `test_p1_smoke.py` |
| HR7 kb_state → ready | `test_kb_state_ready_all_projects` | `test_p22_kb_e2e.py` |
| HR8 rerank lift + both axes | `test_e1_rerank_reorders_search_results`, `test_e2_ask_context_is_rerank_ordered`, `test_e3_community_context_is_reranked`, `test_e4_rerank_lift_metric` | `test_p5_server.py` |
| HR9 MCP embed+rerank only | `test_e5_mcp_query_path_no_generation` | `test_p5_server.py` |
| HR10 dashboard chat haiku-only | `test_e6_dashboard_chat_haiku_only`, `test_chat_stream_sse_sends_done` | `test_p5_server.py` / `test_p4_query.py` |
| HR11 direct-DeepSeek classifier | `TestClassificationCorrectness`, `TestClassificationStability`, `TestCrossProjectMetamorphic` | `test_bpre.py` |
| HR12 no idle LLM spin (think=False) | `TestThermalGuard`, `test_no_8b_model_resident` | `test_mcp_config.py` / `test_p6_daemon.py` |
| HR13 per-member + federated wiki | `test_no_dangling_internal_links`, `test_citations_resolve_on_disk`, `test_every_mermaid_block_is_valid`, `test_deterministic_build_is_byte_identical`, `test_federated_root_gets_federation_index` | `test_wiki_rich.py` |
| HR14 BPRE three-tier extraction | `test_A5c_grpc_edge_count_equivalence`, `test_A5d_llm_linkage_off_by_default`, `test_B5_no_test_file_entry_points`, `test_B6_no_duplicate_process_mermaid`, `test_bpre_ast_uses_tree_sitter_only` | `test_bpre_processes.py`, `test_bpre_ast.py`, `test_no_code_semantic_regex.py` |
| HR15 no-heuristic doctrine | `test_no_code_semantic_regex_in_category_a`, `test_category_b_allowlist_is_exhaustive`, `test_overview_detect_services_uses_bpre_ast`, `test_patterns_no_static_framework_map` | `test_no_code_semantic_regex.py` |
| HR16 5-tier ladder | `test_no_skip_markers_in_live_suite`, `test_no_import_re_in_resolution_path`, `TestGraphRelations`, `TestSearchScopes`, `TestOverviewNewWhats` | `test_no_code_semantic_regex.py`, `test_mcp_tool_matrix.py` |
| HR17 Tier-1.5 value-flow | `test_go_short_var_resolves`, `test_go_const_resolves`, `test_go_literal_fast_path`, `test_python_assignment_resolves`, `test_ts_const_resolves`, `test_js_var_resolves`, `test_true_dynamic_not_in_du` | `test_valueflow_dynamic.py` |
| HR18 Tier-1.75 + Tier-2 | `test_rerank_candidates_single_binds`, `test_rerank_candidates_zero_margin_binds`, `test_rerank_candidates_high_margin_falls_through`, `test_rerank_residue_resolved_items_have_conf_0_8`, `test_escalate_sea_invariant_callee_in_candidates` (@slow), `test_escalate_confidence_at_most_0_7` (@slow), `test_default_model_is_deepseek_v4_flash` (@slow) | `test_rerank_resolution.py`, `test_llm_escalation_ladder.py` |
| HR19 deterministic gating | `TestDeterministicResolution.test_no_llm_edges_when_gates_off`, `test_deterministic_two_runs_same_count`, `test_edge_kinds_are_known_non_llm` (@slow) | `test_deterministic_resolution.py` |
| §16 config inheritance | `test_effective_config_inherits_root_excludes`, `test_iter_files_always_yields_ose_config`, `test_overview_status_includes_config_key` | `test_p22_kb_e2e.py` |
| HR20 partition-quality gate | `test_partition_quality_on_ose`, `test_degenerate_fires_on_all_singleton_graph`, `test_status_includes_hierarchy_quality`, `test_kb_state_demoted_when_degenerate` | `test_hierarchy_quality.py` |
| HR21 federation L3 roll-up | `test_hierarchy_includes_federation_domains_and_quality`, `test_federation_hierarchy_creates_no_edges`, `test_federation_hierarchy_deterministic_with_llm_off`, `test_federation_hierarchy_never_reads_symbols`, `test_global_ask_surfaces_l3_federation_domain` (@slow) | `test_hierarchy_quality.py` |
| HR21 e2e + HR20 metamorphic (June-2026 live validation) | HE1 live L3 build ≤8 themes; HE2 overview(hierarchy) cross-surface; HE3 member_count structural identity; HE4 no-clobber idempotency; HE5 cross-project quality invariants; HE6 two-clique positive oracle; HE7 `_regen_owning_hierarchy` source-guard + behavioral; HE8 FaithLens faithfulness (@slow); HE9 cross-model haiku judge (@slow); HE10 ask(scope=global) end-to-end (@slow) | `test_hierarchy_e2e.py` |
| HR22 structural spine | ST1 zero-token; ST2 dir/file nodes present; ST3 code-less dirs included; ST4 member_count; ST5 determinism MR; ST6 incrementality MR; ST7 no-orphan; ST8 kind→NULL type; SG2 no LLM import in structure.py | `test_structure_hierarchy.py` |
| HR23 DIKW token economy | SC6 producer↔consumer symmetry; SC8a no leidenalg; SC8b detect_communities deterministic; LW1 head sets narrated=1; LW2 structural label leaves narrated=0; LW3 lazy narration 0→1; LW4 idempotency; LW5 unnarrated excluded; LW6 narrated included; LW7 absent from top_communities; LW8 column exists | `test_schema_consistency.py`, `test_lazy_wisdom.py` |
| HR24 k-core hierarchy + adaptive tree-walk | SC8a/SC8b; RR1 global tree-walk header; RR2 architecture tree-walk; RR3 feature no tree-walk; RR4 grounded citations; RR5 adaptive MR; RR6 determinism MR; RR7 empty fallback | `test_schema_consistency.py`, `test_retrieval_routing.py` |

## 15. Design rationale

- **Symlink-based federation** mirrors how developers compose multi-repo workspaces without
  a manifest format to maintain.
- **Members as independent projects** keeps the pipeline uniform; makes incremental updates
  and removals cheap; gives correct results for both whole-workspace and single-repo queries.
- **Per-project content-addressed storage** bounds blast radius; makes vacuum/removal
  trivial.
- **Event-driven + reconcile** is self-healing: stalled projects repaired at startup and on
  demand; edits flow incrementally with debounced enrichment.
- **One daemon over HTTP** removes the per-session ~1 GB engine cost of the stdio bridge.
