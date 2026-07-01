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
  communities (stalled pipeline): `_index_project` + `_enrich_project`. After the
  per-project stall loop, a **root-pass** runs `build_federation_hierarchy(root)` +
  `reconstruct_processes(root)` for every enabled federation root — Enzyme-IVM
  (`child_sig`) and content-sig checks make both no-ops unless member L2 titles or
  source files drifted. Globally pausable via `_PAUSED` (tests set this via the
  `pause_sweeps` autouse fixture).
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
| **HR7** | `kb_state` lifecycle: `indexing → searchable → enriching → ready`; `ready` iff `enriched_pct ≥ 95` (flat-L1 only; L2/L3 deleted WS-B 2026-06-26). Federated entity = worst-of members. |
| **HR8** | Two-stage retrieval at query time: Stage 1 = bi-encoder vector search (`sqlite-vec`); Stage 2 = cross-encoder rerank (`jina-reranker-v1-turbo-en`, GPU). Both AXIS A (code chunks) and AXIS B (community summaries) are reranked. Results ordered by `rerank_score`, never the bare vector `score`. Reranking never runs at index/KB-build time. |
| **HR9** | MCP query actions (`search`/`ask`/`graph`/`overview`) perform ONLY embedding + reranking — no generative LLM (cloud or local). `ask` → `compose_answer` = assembled context; generation is delegated to the calling agent (June 2026 MCP best practice). |
| **HR10** | Dashboard chat (`POST /api/chat_stream`) uses **claude-haiku-4-5** only (Claude Code CLI, `claude -p --model`); **no DeepSeek fallback** — emits an SSE `{"type":"error"}` + `{"type":"done"}` when the `claude` CLI is unavailable. **DeepSeek is KB-enrichment-exclusive** (HR12). **Codex support removed**. Dashboard-only: not wired to any MCP tool. |
| **HR11** | `semantic_type` is assigned by **direct LLM classification (cloud DeepSeek `deepseek-v4-flash`)** over title+summary (batch-20) — **no local fallback**. The daemon classifies only new/unclassified communities (`reclassify_all=False`) → never re-labels settled ones (no churn). The LLM classifier's output is final — HR15 (no-heuristic doctrine) governs; no post-classification size demotion. No embeddings/prototypes in classification. |
| **HR12** | **No local generative LLM** — KB build (`_enrich_project`) uses **cloud DeepSeek only** for all generative tasks (summaries, intents, classification, wiki narrative). The daemon crashes loudly at `_enrich_project` entry if `DEEPSEEK_API_KEY` is absent (`deepseek_key()` returns `None`). GPU lane is embeddings + reranking ONLY. (Supersedes the retired local-LLM idle-spin invariant.) |
| **HR13** | The **wiki is a per-member artifact** built by `build_wiki(store, dir)`: `index.md` (type-grouped ToC), one deterministic `community_{id}.md` per L1 (reused DeepSeek summary as prose, member table with **project-root-relative** source citations, call-graph mermaid drawn from real `edges`). Community pages and diagrams use **no LLM** (deterministic → byte-identical reruns (wiki prose is template-based; no DeepSeek required)); L1 narration calls **cloud DeepSeek** during `_enrich_project`. L2 domain pages (`domain_{id}.md`) **deleted** (WS-B 2026-06-26). A federated root additionally gets `federation.md` via `build_federated_index` — **presentation-only** aggregation of each member's own graph.db (key business communities, semantic_type rollup); it creates/reads **no cross-repo edges** (HR4 preserved). Citations are root-relative so the absolute device path never leaks (public repo). |
| **HR14** | **BPRE process graph is a root-level artifact**: `process_graph.db` lives in `root_process_db(root_path)` (the federation root's index dir), never in any per-member `graph.db` (HR4 preserved). `reconstruct_processes(root_path)` runs only for roots with ≥2 members. **Three-tier extraction model** — **Tier 1 (tree-sitter, `kb/bpre_ast.py`)**: two-pass structural scan reusing `graph/extractor.py` — Pass A discovers the gRPC/proto API surface by mining generated `*.pb.go` (real constructor + registrar names; no hardcoded patterns, no regex); Pass B detects call sites per-file against that surface (gRPC clients/servers, proto imports, pub/sub publish/consume, HTTP routes/clients, status enums). **Tier 2 (LLM linkage, ON when `DEEPSEEK_API_KEY` present)**: DeepSeek resolves config-driven links (JSON-topic IDs, client hosts) tree-sitter cannot statically reach; emitted with `confidence < 1.0` + `_llm` kind suffix; key absent ⇒ tree-sitter-unreachable edges are **absent**, never heuristically approximated. **Tier 3 (cloud DeepSeek)**: D5 rule-text extraction + D6 process narrative (unchanged). D4 process traces are **handler-anchored and deduped**: each process is keyed on an entry handler's reachable symbol set (BFS over intra-service call graph from the handler), not any-edge service adjacency — no duplicate chains, no spurious transitivity, no test-file entry points. The Tier-1 pass is **GPU-free** and byte-identical on repeated runs (without a DeepSeek key, Tier-2 linkage is suppressed ⇒ F1/F2 preserved). Surfaces: `overview(what='process_flows')` returns reconstructed processes when db exists; `GET /api/process/bpmn` exports XSD-valid XML; the dashboard Processes view renders sequenceDiagrams via Mermaid. |
| **HR15** | **Engine-wide no-heuristic doctrine (tree-sitter + LLM only)**: the *only* code that classifies *what the user's code means* uses **tree-sitter** (structural facts) or **LLM** (semantic/linkage facts). No regex, no static/dynamic keyword list, no mapping table may substitute for structural analysis of user code. **Category A — eliminated** (all three sites migrated as of 2026-06-20): A1 `kb/bpre.py` 12 `re.compile` structural patterns → `kb/bpre_ast.py` Tier-1 tree-sitter; A2 `server/_overview.py _detect_services` duplicate gRPC regex → `bpre_ast` Pass-A discovery (single source of truth shared with BPRE); A3 `kb/patterns.py _KNOWN` static framework map → real `graph/llm.deepseek_chat` call (cached per dep-set, raw-dep-name fallback when no key). **Category B — intrinsic mechanism, exempt by name** (updated 2026-06-21 after H1–H3 universal symbol backbone): (a) `graph/extractor.py` — generic call-node detector (node-kind ∋ `"call"`/`"invocation"`), surviving `_MEMBER_KINDS`/`_BRANCH_NODE_KINDS` frozensets, and `tree_sitter_language_pack.process()` + `StructureKind`/`SymbolKind` label-space — tree-sitter's own grammar vocabulary. **Note**: `_TS_LANG`/`_DEF_KINDS`/`_CALL_NODE` **deleted** by H1–H3 (universal `process()` API replaces per-language dicts); (b) `index/discover.py` — `detect_language_from_path` (replaces deleted `_EXT_LANG`/`detect_language` per-extension table), required before any tree-sitter parse; (c) `graph/enrich.py` + `kb/wiki.py` type-order/label tables — the LLM classifier's output vocabulary contract; (d) `server/_overview.py _VALID` + SQL semantic-type filters — API surface enum + LLM-output consumers, not pre-LLM classifiers. **Guard test**: `test_no_code_semantic_regex.py` — `test_no_code_semantic_regex_in_category_a` fails if any `re.compile`/`re.finditer` appears in Category-A paths; `test_category_b_allowlist_is_exhaustive` fails if regex appears outside the explicit allowlist (anti-erosion lock). |
| **HR16** | **Universal 5-tier resolution ladder** (2026-06-21, `84aa9cf`): language-agnostic by construction, strictly monotone confidence 1.0→0.9→0.8→0.7→0.5; each tier sees only the prior tier's residue; the paid LLM only selects from symbolically-admitted candidates. **Tier 1** (`graph/extractor.py`): pack-native `process()` — ANY language; generic call-node detection; conf 1.0 `EXTRACTED`. **Tier 1.5** (`kb/valueflow.py` + `kb/bpre.py`): deterministic intra-procedural value-flow/constant-propagation (non-literal keys: const/var/:=/`Sprintf`→value; insert→dispatch into maps/DI/topics) + IDL/proto FQN cross-language join + generic cascade; conf 0.9 `RESOLVED`; residue emitted not dropped; no regex, no framework vocab. **Tier 1.75** (`kb/resolve_rerank.py`): GPU-local embed→cross-encoder rerank using structural/type context — free (model already warm), ~60× cheaper than LLM localization; single candidate always binds; conf 0.8 `RERANKED`; gate: OSE default. **Tier 2** (`kb/llm_escalation.py` + `kb/bpre.py`): SEA-style LLM — selects from the symbolically-admitted candidate set, never authors; callee verified ∈ index; no self-consistency, no strict-schema; conf 0.7 `_llm`; ON when `DEEPSEEK_API_KEY` present. **Tier 3** (`graph/extractor.py` + `kb/bpre.py`): whole-file LLM on parse-error/empty-structure files only; conf 0.5 `_llm_file`; ON when `DEEPSEEK_API_KEY` present. **Invariants**: zero per-language/per-framework vocab and no `import re` in the resolution path (generic AST node-kind primitives + FQN + embeddings only); without a DeepSeek key, reconstruction is GPU-free, byte-identical, and deterministic. **Pack facts**: `tree-sitter-language-pack==1.9.1` / `tree-sitter==0.25.2`, 306 canonical + alias ≈ 312 langs, typed `process()`, broad-but-not-universal structure coverage. |
| **HR17** | **Deterministic Tier-1.5: intra-procedural value-flow + FQN join** (`kb/valueflow.py`, 2026-06-21). Handles the *dynamic-mapping* case: non-literal call arguments (const/var assignment, `:=` short-var-decl, `Sprintf`, field lookup) are resolved through per-file `{identifier → string_value}` def-use maps built by a single AST pass. Language coverage: Go (`const_spec`/`var_spec`/`short_var_declaration`), Python (`assignment`), JS/TS (`variable_declarator`), Java/Kotlin (`local_variable_declaration`/`property_declaration`). `resolve_first_arg` follows literal fast-path → def-use identifier → selector field; returns `None` for true dynamics (call-result, reflection) → falls to GPU-rank. FQN join: cross-language edge resolved by IDL/proto fully-qualified name (conf 0.9 `RESOLVED`). Residue is emitted as candidates (not dropped) for Tier-1.75 disambiguation. **Strictly no regex, no framework vocab**: structural AST-node-kind primitives only. Feasibility: YASA (arXiv:2601.17390) build-free data-flow at 31.8 KLOC/min. |
| **HR18** | **GPU Tier-1.75 + SEA-style verified LLM + V4 Flash** (`kb/resolve_rerank.py` + `kb/llm_escalation.py` + `graph/llm.py`, 2026-06-21). Tier-1.75: `rerank_candidates(query_context, candidates, *, margin=0.05)` — GPU cross-encoder (already warm); single candidate always binds; multi-candidate binds when gap ≥ margin; falls through to Tier-2 when gap < margin. `rerank_residue` applies Tier-1.75 across residue items; resolved carry conf=0.8. SweRank (arXiv:2505.07849): retrieve-and-rerank beats Claude-3.5 localization at ~60× lower cost; SACL (arXiv:2506.20081): structural/type context (not bare names) improves reranker precision. Tier-2 (`escalate`): SEA-style (arXiv:2408.04344) — LLM selects from the admitted set, never authors; callee verified ∈ index; `stable_prefix` cache (DeepSeek 384K context, `json_object` non-thinking); no self-consistency, no strict-schema enforcement. Model: `deepseek-v4-flash` (pinned; `deepseek-chat` alias deprecates 2026-07-24); override via `OSE_DEEPSEEK_MODEL`. Cache stats exposed at `/api/metrics` as `llm_cache.{hits,misses,calls}`. |
| **HR19** | **DEFERRED — cross-service recall complement** (documented, not built). Static IDL/FQN-join = static SOTA with a finite recall floor. Next-phase lift: (a) test-time OpenTelemetry traces via servicegraph connector — confirms dynamic paths at ~0.95; (b) gRPC reflection at runtime — resolves service-to-endpoint bindings without generated code. Reconciliation: trace-confirmed-static / trace-only (dynamic-flag) / static-only. Anti-hallucination guard (arXiv:2512.12117): structural verification gate stays mandatory even with trace data. |
| **HR20** | **Composite partition-quality gate on `kb_state`** (`graph/quality.py`, 2026-06-22; updated 2026-06-25). `partition_quality(store)` is deterministic (igraph + SQL, zero LLM) and computes a composite score: `modularity_q` (igraph.modularity), `coverage` (intra-community edges / total), `singleton_ratio` (L1 with member_count==1 / n_L1). `degenerate=True` when `(edges>0 AND singleton_ratio ≥ 0.60) OR (edges>0 AND coverage < 0.20) OR (edges>0 AND n_l1≥2 AND modularity_q < 0.05)`. **Edge-free projects (ec=0) are exempt from the entire degeneracy gate** — all three clauses require `edges>0`; an edge-free repo structurally cannot form non-singleton communities via detection so no penalty applies. A degenerate partition demotes `kb_state` from `'ready'` to `'searchable'` in `overview(status)`. **Modularity Q alone is explicitly rejected** as the sole gate (exponentially many near-optimal partitions, degrades on sparse code graphs; CPM preferred for code — arXiv 2501.07025). Federation roots with synthesis L3 communities (0 call edges) are exempt from `symbol_hollow`. |
| ~~**HR21**~~ | **DELETED** (WS-B 2026-06-26): Federation-global L3 roll-up synthesis (`kb/federation_hierarchy.py`) removed; L3 rows purged from all graph.db; `build_federation_hierarchy` gone. |
| ~~**HR22**~~ | **DELETED** (WS-B 2026-06-26): Deterministic structural spine (`kb/structure.py`) removed; level=0 dir/file community rows purged; `build_structure_tree` gone. |
| **HR23** | **DIKW token economy** (`graph/enrich.py`, 2026-06-23; updated WS-B 2026-06-26). Spend LLM tokens only to climb Information→Knowledge→Wisdom, and only at the nodes/queries actually read. **Information** (L1 fastgreedy community structure) = 0 tokens, byte-reproducible. **Knowledge** (LLM on the significance-gated head only): `compute_significance()` classifies all unenriched L1 into head (`member_count≥8 OR ≥2 cross-community edges`) and tail; head is enriched in batches of 20 via `deepseek_extract(stable_prefix, dynamic_tail)` (prefix-cached), capped by `OSE_ENRICH_BUDGET_TOKENS`. `_enrich_one_batch` sets `narrated=1` on every successfully enriched community (idempotency guard). **Abstention on unrecognised types**: when DeepSeek returns a type not in `_TYPE_ORDER`, `semantic_type` is set to `NULL` (reject-option doctrine, NCwR arXiv 2412.03190) — never forced to `"utility"`. **Tail abstains**: `label_community_structural` sets `semantic_type=NULL, narrated=0`. **`narrated` column** (`communities.narrated INTEGER DEFAULT 0`): 0 = unnarrated (tail or not-yet-narrated); 1 = LLM-narrated (head or lazily-narrated). All semantic retrieval selectors filter `narrated=1`. `llm_token_stats()` (flat dotted-namespace keys) exposes the full token budget at `/api/metrics` and `overview(what='metrics')`. Active namespaces: `enrich.*` (L1 head batched narration), `classify.*` (semantic-type classification), `bpre.*` (BPRE process narrative batched), `bpre_link.*` (BPRE Tier-2 edge-linking `_llm_link_resolve`, wired 2026-07-01 — previously called `deepseek_extract` without accumulation, invisible to the budget). *(L2/L3 `l2.*`/`l3.*` namespaces deleted WS-B 2026-06-26.)* `classify.calls==0` invariant: the tail abstains, so classify only fires on narrated head rows. |
| **HR24** | **Flat-L1 community detection: fastgreedy modularity** (`graph/community.py`, 2026-06-23; updated WS-B 2026-06-26). `detect_communities` uses `igraph.community_fastgreedy().as_clustering()` (Clauset-Newman-Moore, agglomerative-greedy) on the symbol call-graph's undirected subgraph; edgeless symbols grouped by directory (avoids N-singleton explosion). RNG seeded at module import for byte-identical runs (`random.seed(0)` + `igraph.set_random_number_generator`). All output rows are `level=1`. Version constant: `ALGO_VERSION = "fg1"` in `community.py`. **`leidenalg` must not be imported** in `community.py` — SC8a guard enforces this. **SC8b guard**: `detect_communities` is byte-identical on two independent runs from the same graph. **Flat reasoning-retrieval** (`query/ask.py`, updated WS-B): `_tree_walk_context()` selects top-k `narrated=1` L1 communities via GPU cross-encoder rerank (no L2 `parent_id` drill) — used for `global` and `architecture` scopes. *(L2 `build_hierarchy` + `kb/hierarchy.py` + adaptive tree-walk deleted WS-B 2026-06-26.)* |
| **HR25** | **Self-healing graph pipeline** (`graph/store.py` meta table; `daemon/sweeps.py` M1-M2, 2026-06-23; updated WS-B 2026-06-26). **M1 — version + source stamps**: `graph.db` carries a `meta(key,value)` table (survives `GraphStore.clear()`). Two keys: `algo_version = f"{ALGO_VERSION}+{_code_fingerprint()}"` (bumped by editing `ALGO_VERSION`) and `source_sig` (SHA-1 of sorted `relpath:mtime` for `iter_files(root, federation_mode=True)` — stat-only, GPU-free). Both stamped by `_index_project` after `detect_communities` and by `_rederive_graph` after re-derive. **M2 — reconcile self-heals stale graphs**: `reconcile_projects` calls `_graph_stale(path, gs)` in the non-federation branch; when `True`, calls `_rederive_graph(project_path)` + `_enrich_project`. `_rederive_graph` is GPU-free (tree-sitter + igraph): clears graph, re-extracts symbols+edges via `_extract_graph(gs, root)`, re-detects L1 communities, restamps both meta keys. Rides the existing 30-min `_reconcile_loop` — no new timers. **GPU-only invariant preserved**: `_rederive_graph` never calls `get_embedder` or `embed()`. *(M3 L2 coarseness guard + M4 edge-sparse L2 completeness + `_regen_owning_hierarchy` L3 heal deleted WS-B 2026-06-26.)* |
| **HR26** | **GPU execution provider auto-detect** (`core/gpu.py`, 2026-06-23). `_GPU_EP_ORDER` whitelist (CPU excluded): `NvTensorRTRTXExecutionProvider → TensorrtExecutionProvider → CUDAExecutionProvider → MIGraphXExecutionProvider → ROCMExecutionProvider → DmlExecutionProvider`. `rank_gpu_providers(available, *, disable_tensorrt)` is pure (deterministic, no GPU). `select_gpu_device()` (@lru_cache): sets `CUDA_DEVICE_ORDER=PCI_BUS_ID` before CUDA init, enumerates via `pynvml` (max free VRAM → tie-break compute capability → lowest index), honours `OPENCODE_GPU_DEVICE`. `select_gpu_providers()`: preloads ORT NVIDIA DLLs, ranks real `ort.get_available_providers()`, **raises `RuntimeError` if empty** (CPU forbidden — fatal). Both `Embedder` and `Reranker` call `assert_gpu_available()` in `__init__` and bind via `select_gpu_providers()` in `_init()`; session `add_session_config_entry("session.disable_cpu_ep_fallback", "1")` prevents ORT silent CPU binding; binding guard asserts `session.get_providers()[0] ∈ GPU_EP_NAMES`. `assert_gpu_available` / `is_gpu_available` are the renamed backward-compat aliases. |

| **HR27** | **Docgen + OKF: LLM-native via `claude -p`, manual-trigger only** (2026-06-26, Phase 2+2c). `run_docgen(project_path)` and `run_okf(project_path)` both call `claude -p` headless (Haiku/Sonnet) to read the repo source directly and author IH pages / OKF concept files. **No deterministic skeleton** — kill-switch `OSE_DOCGEN=0`/`OSE_OKF=0` → no output (CI hermeticity via committed golden fixtures, not a skeleton generator). **No tree-sitter on the doc-tooling path** (P12). **Manual-trigger only**: `opencode-search docgen <path>` CLI or `POST /api/docgen`; never called from `_enrich_project` (auto-sweep) or MCP tools (P13). **MCP surface = index + search/ask/graph/overview only** — docgen and okf are absent from `_MCP_TOOLS`. Federation-member cleanup (remove `_meta/provenance.json`-marked generated files, preserve human docs) continues at member paths via `_cleanup_generated_docs`. Sweep script: `scripts/sweep_docgen.py`. HR4 preserved: no cross-repo edges. |
| **HR28** | **Docgen output re-indexed under `scope=docs`** (2026-06-25). After `run_docgen(root)`, `index_docs(root, embedder, store)` embeds generated `docs/` pages using `detect_language(p) in _TEXT_LANGS` — idempotent per-path replace. Makes docgen pages searchable via `search(scope="docs")` or `search(scope="all")`; `scope="code"` explicitly excludes `_TEXT_LANGS` chunks (one-line guard in `query/search.py`). The watcher gate + `_source_fingerprint` keep excluding `docs/` from the code-index path — no churn loop. |
| **HR29** | **`.opencode-index.yaml` honored by every project-source enumerator** (2026-06-25). The three active enumerator paths (incremental/watcher path, `kb/bpre.py::_source_files`, `kb/bpre_ast.py::federation_discover`) all route through `effective_config(root)` + `is_excluded(path, cfg.exclude, root)`. `iter_files` remains the canonical enumerator. No enumerator may silently index a file that `effective_config` would exclude. (`kb/structure.py` enumerator deleted WS-B 2026-06-26.) (See §16.5 in Part 1.) |
| **HR30** | **MCP surface integrity** (2026-06-26, Phase 2c). The MCP tool surface must be exactly `index + search + ask + graph + overview`; `docgen` and `okf` must be absent from `_MCP_TOOLS`. *(Old HR30 = `kb/world_model.py` RTM was deleted WS-B; identifier re-used.)* Guard: `test_api_docgen_not_in_mcp` in `test_docgen_dashboard.py`. |
| **HR31** | **LLM lane map — four-lane separation** (2026-06-26, Phase 4). The four inference lanes must not cross: **GPU** (FastEmbed/ONNX/CUDA) = embed + cross-encoder rerank ONLY (CPU fallback fatal); **DeepSeek** (cloud `deepseek-v4-flash`) = KB enrichment (community narration, wiki, BPRE process narrative, LLM edge linkage); **claude-haiku-4-5** (cloud) = dashboard chat only; **`claude -p`** (Haiku/Sonnet, headless) = doc-tooling (docgen IH + OKF). No cross-lane calls — docgen/okf must not call DeepSeek; chat must not call DeepSeek; KB enrichment must not call `claude -p`. All LLM lanes (DeepSeek narration/linkage/file-pass) are ON when `DEEPSEEK_API_KEY` is present and suppressed naturally when absent. `OSE_DOCGEN=0` / `OSE_OKF=0` remain as opt-outs for doc-tooling. Guard: `test_inference_lanes.py`. |

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
9. **MCP query actions never generate** — the dashboard chat LLM (**claude-haiku-4-5** only, no DeepSeek fallback) is
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
12. **Dashboard chat = claude-haiku-4-5 only** via the Claude Code CLI; **no DeepSeek fallback** — emits SSE error when CLI unavailable; **codex removed**. (≡ HR10.)

| **HR32** | **Idle-efficiency (source-drift gate + idle-unload + reconcile bulkification)** (2026-07-01). `on_change` in `daemon/sweeps.py` computes `_source_fingerprint(project_path)` after the index step and skips the entire KB cascade (enrich/wiki/BPRE) when the sig equals `_last_enriched_sig[path]`. The sig is committed to `_last_enriched_sig` only after a successful `_enrich_project` so a failed pass retries. Metadata-only events (watchdog `"closed"`/`"opened"` event types) are filtered at the `_H.on_any_event` boundary before reaching `on_change`. Together these stop the every-63 s spurious `reconstruct_processes` cascade that was pinning one CPU core. With no cascade the daemon reaches true idle → `_idle_unload` (300 s) nulls `_embedder`/`_reranker`/`_default`, calls `gc.collect` + `malloc_trim`, and returns the ORT CUDA arena to the OS — the only reliable RSS release path. **Startup-reconcile bulkification (Part D, 2026-07-01)**: `reconcile_projects()` sets `_reconcile_active` for the duration of a bulk pass; `_enrich_project` skips its per-member BPRE fan-out (`_regen_owning_processes` + self-BPRE) while it is set, so the reconcile federation root-pass — now unconditional, trusting `reconstruct_processes`' own persistent `bpre_algo`/`bpre_source_sig` stamp instead of a restart-fragile in-memory sig gate — is the sole BPRE trigger during a bulk pass (no more rebuilding the same root repeatedly on a mid-pass-shifting sig). A daemon-wide `_KB_HEAVY_LOCK` serializes all CPU-bound KB work (enrich body + root-pass BPRE) across the watcher and reconcile threads, so at most one heavy pass — never two — runs at a time. **Target**: < 1 % CPU at idle; a restart's one-time settle stays bounded to ~one core (never two concurrent BPRE passes) with no swap growth; RSS drops to Python+uvicorn+sqlite floor; GPU still serves embed/rerank on real queries. |
| **HR33** | **Notification-first watcher contract** (2026-07-01). `Watcher.start()` attempts `_try_inotify()` first (watchdog/inotify): if successful `self._observer` is set and the poll thread is NOT started (`self._thread = None`). The poll fallback `_loop` (5 s cadence) starts only when inotify is unavailable (NFS/SMB, `max_user_watches` exhaustion). The snapshot dict is seeded lazily on `_loop`'s first pass (not in `watch()`) so the inotify path incurs no `iter_files` stat-walk at registration time. Fallback degradation is logged at `WARNING` level so it appears in the journal. Metadata-only event types (`"closed"`, `"opened"`) are dropped before the project debounce so they never wake the cascade thread. fanotify requires root → user-service daemon stays on inotify. |
| **HR35** | **Gitignore/hidden-dir-aware discovery with OSE-config precedence** (2026-07-01). `_source_fingerprint` (`daemon/sweeps.py`) and the watcher's `is_ignored_path` both route through one shared resolver, `index/discover.py::_should_drop`, applied in strict order per path segment: (1) OSE `.opencode-index.yaml` `exclude` → **drop** (strongest, explicit intent); (2) OSE `include` (new force-keep globs) → **keep**, overriding everything below — this is what makes OSE config win over `.gitignore` on conflict; (3) default policy (`cfg.use_default_ignores`): `IGNORED_DIRS` membership or a hidden segment (`.`-prefixed, below the project root) → **drop**; (4) `.gitignore` (new, supplementary, gated by `cfg.respect_gitignore`, default `True`; `pathspec.PathSpec.from_lines("gitignore", ...)`, cached per-file keyed on mtime, root + nested chains honored) → **drop**; (5) else **keep**. Root-caused 2026-07-01: `iter_files` had no hidden-dir skip and no `.gitignore` support, so a live `vite dev` + Playwright-MCP session continuously rewriting git-ignored `wiki/.svelte-kit`/`.playwright-mcp` flipped `_source_fingerprint` on every write, making the P4/HR32 drift gate perpetually report "drifted" and re-triggering the full BPRE/enrich cascade every ~5 min — pinning a CPU core indefinitely and blocking `_idle_unload`. `IGNORED_DIRS` also gained an explicit tool-cache belt (`.svelte-kit`, `.playwright-mcp`, `.astro`, `.turbo`, `.parcel-cache`, `.vite`, `.output`, `.vitest`) for clarity on non-git projects. The fix adds no idle cost: gitignore specs are compiled once per file and cached on mtime; hidden-dir/`IGNORED_DIRS` checks are pure string comparisons. Verified live on the originally-affected repo by direct causal isolation: `iter_files` yields zero `.svelte-kit`/`.playwright-mcp` entries, and every observed post-fix fingerprint change traced to a real, tracked, actively-edited source file — never to a gitignored/hidden path. |
| **HR36** | **BPRE code-only, discovery-unified reuse signature** (2026-07-01). BPRE's federation-wide reuse stamp (`bpre_source_sig`, `kb/bpre.py`) and per-member scan-cache key (`_member_scan_sig`) previously hashed `_source_fingerprint` — the same all-files sig HR35 corrected for the indexer — but via a *separate, weaker* walk (`_source_files` used raw `os.walk` + `IGNORED_DIRS`, no hidden-dir/gitignore awareness). Fix: `_source_files` now routes through the same HR35 `_should_drop` resolver as `iter_files`, filtered to `is_code_language(detect_language(p))` (preserving `_is_test_file` exclusion and nested-federation-member pruning); `_bpre_code_sig(member)` hashes only that code-file set, coarse-cached on the member root-dir mtime (mirrors `_source_fingerprint`'s cache pattern) and invalidated by `daemon/sweeps.py::on_change` alongside `_fingerprint_cache`. `bpre_source_sig` and `_member_scan_sig` are repointed to `_bpre_code_sig`; the stamp is written once from the sig computed at rebuild start (`_reconstruct_processes_locked`), removing the prior end-of-rebuild recompute that chased a moving target. Root-caused 2026-07-01 as a 3rd, distinct idle-CPU cause — found live *after* HR35 shipped: on a 170-member federation root, concurrent docs/config/image edits (`docs/*.yaml`, `docs/**/*.png`) and hidden `.claude/*.js` tool-cache churn (both outside BPRE's actual dependency surface but inside the all-files fingerprint) kept flipping `bpre_source_sig` faster than the ~5 min federation-wide BPRE rebuild it triggered could finish, producing back-to-back rebuilds and pinning a CPU core continuously. Adds no idle cost (same coarse-cache-plus-explicit-invalidation pattern as HR32/HR35); a real member code edit still flips the sig and rebuilds correctly. |

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
| HR6 GPU-only | `test_no_cpu_fallback`, `test_embedder_bound_to_gpu` | `test_p1_smoke.py` |
| HR7 kb_state → ready | `test_kb_state_ready_all_projects` | `test_p22_kb_e2e.py` |
| HR8 rerank lift + both axes | `test_e1_rerank_reorders_search_results`, `test_e2_ask_context_is_rerank_ordered`, `test_e3_community_context_is_reranked`, `test_e4_rerank_lift_metric` | `test_p5_server.py` |
| HR9 MCP embed+rerank only | `test_e5_mcp_query_path_no_generation` | `test_p5_server.py` |
| HR10 dashboard chat haiku-only | `test_e6_dashboard_chat_haiku_only`, `test_chat_stream_sse_sends_done` | `test_p5_server.py` / `test_p4_query.py` |
| HR11 direct-DeepSeek classifier | `TestClassificationCorrectness`, `TestClassificationStability`, `TestCrossProjectMetamorphic` | `test_bpre.py` |
| HR12 no idle LLM spin (think=False) | `test_no_local_generative_llm_in_llm_module`, `test_no_local_llm_tokens_anywhere_in_src` | `test_inference_lanes.py` |
| HR13 per-member + federated wiki | `test_no_dangling_internal_links`, `test_citations_resolve_on_disk`, `test_every_mermaid_block_is_valid`, `test_deterministic_build_is_byte_identical`, `test_federated_root_gets_federation_index` | `test_wiki_rich.py` |
| HR14 BPRE three-tier extraction | `test_A5c_grpc_entry_matches_edges`, `test_A5d_llm_linkage_off_by_default`, `test_B5_no_test_file_entry_points`, `test_B6_no_duplicate_process_mermaid`, `test_bpre_ast_uses_tree_sitter_only` | `test_bpre_processes.py`, `test_bpre_ast.py`, `test_no_code_semantic_regex.py` |
| HR15 no-heuristic doctrine | `test_no_code_semantic_regex_in_category_a`, `test_category_b_allowlist_is_exhaustive`, `test_overview_detect_services_uses_bpre_ast`, `test_patterns_no_static_framework_map` | `test_no_code_semantic_regex.py` |
| HR16 5-tier ladder | `test_no_skip_markers_in_live_suite`, `test_no_import_re_in_resolution_path`, `TestGraphRelations`, `TestSearchScopes`, `TestOverviewNewWhats` | `test_no_code_semantic_regex.py`, `test_mcp_tool_matrix.py` |
| HR17 Tier-1.5 value-flow | `test_go_short_var_resolves`, `test_go_const_resolves`, `test_go_literal_fast_path`, `test_python_assignment_resolves`, `test_ts_const_resolves`, `test_js_var_resolves`, `test_true_dynamic_not_in_du` | `test_valueflow_dynamic.py` |
| HR18 Tier-1.75 + Tier-2 | `test_rerank_candidates_single_binds`, `test_rerank_candidates_zero_margin_binds`, `test_rerank_candidates_high_margin_falls_through`, `test_rerank_residue_resolved_items_have_conf_0_8`, `test_escalate_sea_invariant_callee_in_candidates` (@slow), `test_escalate_confidence_at_most_0_7` (@slow), `test_default_model_is_deepseek_v4_flash` (@slow) | `test_rerank_resolution.py`, `test_llm_escalation_ladder.py` |
| HR19 deterministic gating | `TestDeterministicResolution.test_no_llm_edges_when_gates_off`, `test_deterministic_two_runs_same_count`, `test_edge_kinds_are_known_non_llm` (@slow) | `test_deterministic_resolution.py` |
| §16 config inheritance | `test_effective_config_inherits_root_excludes`, `test_iter_files_always_yields_ose_config`, `test_overview_status_includes_config_key` | `test_p22_kb_e2e.py` |
| HR20 partition-quality gate | `test_partition_quality_on_ose`, `test_edge_free_graph_not_degenerate` (DQ1), `test_degenerate_fires_on_all_singleton_graph`, `test_status_includes_hierarchy_quality`, `test_kb_state_demoted_when_degenerate` | `test_hierarchy_quality.py` |
| ~~HR21 federation L3 roll-up~~ | **DELETED** (WS-B 2026-06-26) | ~~`test_hierarchy_quality.py`~~ |
| ~~HR21 e2e + HR20 metamorphic~~ | **DELETED** (WS-B 2026-06-26) | ~~`test_hierarchy_e2e.py`~~ |
| ~~HR22 structural spine~~ | **DELETED** (WS-B 2026-06-26) | ~~`test_structure_hierarchy.py`~~ |
| HR23 DIKW token economy (flat-L1) | SC8a no leidenalg; SC8b detect_communities deterministic; LW1 head sets narrated=1; LW4 idempotency; LW5 unnarrated excluded; LW6 narrated included; LW8 column exists *(LW2 structural label: deleted with structure.py WS-B)* | `test_schema_consistency.py`, `test_lazy_wisdom.py` |
| HR24 flat-L1 community detection + flat tree-walk | SC8a/SC8b; RR1 global tree-walk header; RR2 architecture tree-walk; RR3 feature no tree-walk; RR6 determinism MR; RR7 empty fallback *(RR4 grounded citations from L2 tree: deleted WS-B)* | `test_schema_consistency.py`, `test_retrieval_routing.py` |
| HR25 self-healing graph pipeline | `test_algo_drift_triggers_rederive`, `test_source_drift_triggers_rederive`, `test_rederive_graph_has_no_embedder_call`, `test_graph_stale_fires_on_poisoned_version` | `test_self_heal_e2e.py`, `test_self_heal.py` |
| HR26 GPU provider autodetect | `test_rank_gpu_providers_ladder_order`, `test_select_gpu_providers_non_empty_and_no_cpu`, `test_select_gpu_providers_fatal_on_cpu_only` | `test_gpu_autodetect.py` |
| HR27 root-only docgen + member cleanup | GT1 (`test_gt1a_is_federation_member_truth_table`), GT2 (`test_gt2_cleanup_member_docs_synthetic`), CT1 (mixed docs cleanup), CT2 (all-generated removal) | `test_docgen_rootonly.py` |
| HR28 docs re-indexed scope=docs | GG1 (include-decoupling), GG2 (round-trip embed+search), GG3 (scope isolation), GG4 (churn-guard fingerprint unchanged) | `test_docs_index.py` |
| HR29 config universality — every enumerator | HH1 (full-index baseline), HH2 (`test_hh2_on_change_filters_excluded`), HH3 (cross-surface exclusion: spine + BPRE) | `test_config_universality.py` |
| HR30 MCP surface integrity | `test_api_docgen_not_in_mcp` (class method in `TestDocgenDashboard`) | `test_docgen_dashboard.py` |
| HR32 idle-efficiency (source-drift gate + idle-unload) | `test_drift_gate_skips_enrich_when_sig_unchanged`, `test_drift_gate_triggers_enrich_when_sig_changes` | `test_idle_stability.py` |
| HR32 reconcile bulkification (Part D: cascade suppression + single-flight lock) | `test_reconcile_active_flag_lifecycle`, `test_reconcile_bpre_root_pass_unconditional`, `test_bulk_reconcile_suppresses_member_bpre_fanout`, `test_kb_heavy_lock_serializes_concurrent_passes` | `test_idle_stability.py` |
| HR33 notification-first watcher contract | `test_watcher_prefers_inotify_over_poll` | `test_idle_stability.py` |
| HR35 gitignore/hidden-dir-aware discovery with OSE-config precedence | `test_gitignore_respected_root_and_nested`, `test_hidden_dir_skip_tool_caches`, `test_include_overrides_gitignore_exclude_beats_include`, `test_respect_gitignore_false_disables_gitignore_only`, `test_drift_gate_quiescent_under_tool_cache_churn`, `test_is_ignored_path_agrees_with_iter_files` | `test_idle_stability.py` |
| HR36 BPRE code-only, discovery-unified reuse signature | `test_bps1_docs_churn_quiescent_no_rebuild`, `test_bps2_hidden_dir_tool_cache_churn_no_rebuild`, `test_bps3_real_code_drift_still_rebuilds`, `test_bps4_convergence_second_call_reuses` | `test_idle_stability.py` |
| L3 traceability guard | `test_l3_rtm_all_tests_resolve` — machine-verifies every `model.yaml` L3 `test:` name resolves to a live `def test_…` | `test_world_model_traceability.py` |

## 15. Design rationale

The *engineering principles* that govern all architectural choices are recorded as a first-class doctrine register in the companion document's §1a — see [federation-and-search-engine.md §1a](federation-and-search-engine.md#1a-engineering-principles-doctrine).

- **Symlink-based federation** mirrors how developers compose multi-repo workspaces without
  a manifest format to maintain.
- **Members as independent projects** keeps the pipeline uniform; makes incremental updates
  and removals cheap; gives correct results for both whole-workspace and single-repo queries.
- **Per-project content-addressed storage** bounds blast radius; makes vacuum/removal
  trivial.
- **Event-driven + reconcile** is self-healing: stalled projects repaired at startup and on
  demand; edits flow incrementally with debounced enrichment.
- **One daemon over HTTP** removes the per-session ~1 GB engine cost of the stdio bridge.
