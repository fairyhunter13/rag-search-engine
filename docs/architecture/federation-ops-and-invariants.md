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
| **HR1** | Watcher is the steady-state indexing trigger; `on_change` incremental-embeds changed files. Full `_index_project` (graph+Leiden) runs only at first index / reconcile. |
| **HR2** | KB enrichment is triggered by the same watcher event on its own 45s/project debounce. Event-driven only — no `kb_sweep` / periodic reconcile timer. |
| **HR3** | Re-running `detect_communities` / `_enrich_project` MUST NOT wipe existing summaries. `ready` stays `ready` across re-index (`summary=None` in `upsert_community`, never `""`). |
| **HR4** | Federation = query-time union; each member has its own stores; no cross-repo edges. Fan-out via `expand_federation`/`federated_map`. (≡ inv#1–#4.) |
| **HR5** | One absolute path = one index dir. Two distinct clones → two indexes. (≡ inv#2, #8.) |
| **HR6** | GPU-only for **embeddings + reranking** (FastEmbed/ONNX/CUDA); any CPU fallback raises fatally. No local generative LLM — the GPU lane is embeddings + reranking ONLY. (≡ inv#5.) |
| **HR7** | `kb_state` lifecycle: `indexing → searchable → enriching → ready`; `ready` iff `enriched_pct ≥ 95 AND l2_enriched_pct == 100`. Federated entity = worst-of members. |
| **HR8** | Two-stage retrieval at query time: Stage 1 = bi-encoder vector search (`sqlite-vec`); Stage 2 = cross-encoder rerank (`jina-reranker-v1-turbo-en`, GPU). Both AXIS A (code chunks) and AXIS B (community summaries) are reranked. Results ordered by `rerank_score`, never the bare vector `score`. Reranking never runs at index/KB-build time. |
| **HR9** | MCP query actions (`search`/`ask`/`graph`/`overview`) perform ONLY embedding + reranking — no generative LLM (cloud or local). `ask` → `compose_answer` = assembled context; generation is delegated to the calling agent (June 2026 MCP best practice). |
| **HR10** | Dashboard chat (`POST /api/chat_stream`) uses **claude-haiku-4-5** as primary (Claude Code CLI, `claude -p --model`); **DeepSeek fallback** when haiku CLI absent or returns empty output. **Codex support removed**. Dashboard-only: not wired to any MCP tool. |
| **HR11** | `semantic_type` is assigned by **direct LLM classification (cloud DeepSeek `deepseek-chat`)** over title+summary (batch-20) — **no local fallback**. The daemon classifies only new/unclassified communities (`reclassify_all=False`) → never re-labels settled ones (no churn). A `<3`-member community cannot be `business_process`/`business_rule` (structural guard → `feature`). No embeddings/prototypes in classification. |
| **HR12** | **No local generative LLM** — KB build (`_enrich_project`) uses **cloud DeepSeek only** for all generative tasks (summaries, intents, classification, wiki narrative). The daemon crashes loudly at `_enrich_project` entry if `DEEPSEEK_API_KEY` is absent (`deepseek_key()` returns `None`). GPU lane is embeddings + reranking ONLY. (Supersedes the retired local-LLM idle-spin invariant.) |
| **HR13** | The **wiki is a per-member artifact** built by `build_wiki(store, dir)`: `index.md` (type-grouped ToC), one deterministic `community_{id}.md` per L1 (reused DeepSeek summary as prose, member table with **project-root-relative** source citations, call-graph mermaid drawn from real `edges`), and one `domain_{id}.md` per L2 (DeepSeek narrative, templated fallback). Community pages and diagrams use **no LLM** (deterministic → byte-identical reruns with `OSE_WIKI_LLM=0`); only L2 narrative calls **cloud DeepSeek** — no embedder, no local server. A federated root additionally gets `federation.md` via `build_federated_index` — **presentation-only** aggregation of each member's own graph.db (domains, key business communities, semantic_type rollup); it creates/reads **no cross-repo edges** (HR4 preserved). Citations are root-relative so the absolute device path never leaks (public repo). |
| **HR14** | **BPRE process graph is a root-level artifact**: `process_graph.db` lives in `root_process_db(root_path)` (the federation root's index dir), never in any per-member `graph.db` (HR4 preserved). `reconstruct_processes(root_path)` runs only for roots with ≥2 members. **Three-tier extraction model** — **Tier 1 (tree-sitter, `kb/bpre_ast.py`)**: two-pass structural scan reusing `graph/extractor.py` — Pass A discovers the gRPC/proto API surface by mining generated `*.pb.go` (real constructor + registrar names; no hardcoded patterns, no regex); Pass B detects call sites per-file against that surface (gRPC clients/servers, proto imports, pub/sub publish/consume, HTTP routes/clients, status enums). **Tier 2 (opt-in LLM linkage, `OSE_BPRE_LLM_LINK`, default OFF)**: when enabled, DeepSeek resolves config-driven links (JSON-topic IDs, client hosts) tree-sitter cannot statically reach; emitted with `confidence < 1.0` + `_llm` kind suffix; flag OFF ⇒ tree-sitter-unreachable edges are **absent**, never heuristically approximated. **Tier 3 (cloud DeepSeek)**: D5 rule-text extraction + D6 process narrative (unchanged). D4 process traces are **handler-anchored and deduped**: each process is keyed on an entry handler's reachable symbol set (BFS over intra-service call graph from the handler), not any-edge service adjacency — no duplicate chains, no spurious transitivity, no test-file entry points. The Tier-1 pass is **GPU-free** and byte-identical on repeated runs (`OSE_WIKI_LLM=0`; `OSE_BPRE_LLM_LINK` unset ⇒ F1/F2 preserved). Surfaces: `overview(what='process_flows')` returns reconstructed processes when db exists; `GET /api/process/bpmn` exports XSD-valid XML; the dashboard Processes view renders sequenceDiagrams via Mermaid. |
| **HR15** | **Engine-wide no-heuristic doctrine (tree-sitter + LLM only)**: the *only* code that classifies *what the user's code means* uses **tree-sitter** (structural facts) or **LLM** (semantic/linkage facts). No regex, no static/dynamic keyword list, no mapping table may substitute for structural analysis of user code. **Category A — eliminated** (all three sites migrated as of 2026-06-20): A1 `kb/bpre.py` 12 `re.compile` structural patterns → `kb/bpre_ast.py` Tier-1 tree-sitter; A2 `server/_overview.py _detect_services` duplicate gRPC regex → `bpre_ast` Pass-A discovery (single source of truth shared with BPRE); A3 `kb/patterns.py _KNOWN` static framework map → real `graph/llm.deepseek_chat` call (cached per dep-set, raw-dep-name fallback when no key). **Category B — intrinsic mechanism, exempt by name**: `graph/extractor.py` node-kind tables (`_TS_LANG`/`_DEF_KINDS`/`_CALL_NODE`/`_MEMBER_KINDS`/`_BRANCH_NODE_KINDS`) — tree-sitter's own grammar vocabulary; `index/discover.py` extension→language bootstrap (`_EXT_LANG`/`detect_language`) — required before any tree-sitter parse, deterministic by design; `graph/enrich.py` + `kb/wiki.py` type-order/label tables — the LLM classifier's output vocabulary contract; `server/_overview.py _VALID` + SQL semantic-type filters — API surface enum + LLM-output consumers, not pre-LLM classifiers. **Guard test**: `test_no_code_semantic_regex.py` — `test_no_code_semantic_regex_in_category_a` fails if any `re.compile`/`re.finditer` appears in Category-A paths; `test_category_b_allowlist_is_exhaustive` fails if regex appears outside the explicit allowlist (anti-erosion lock). |

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
| §16 config inheritance | `test_effective_config_inherits_root_excludes`, `test_iter_files_always_yields_ose_config`, `test_overview_status_includes_config_key` | `test_p22_kb_e2e.py` |

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
