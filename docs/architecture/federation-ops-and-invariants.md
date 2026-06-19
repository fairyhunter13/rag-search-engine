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
| codex `config.toml` | `url = "http://127.0.0.1:8765/mcp"` (no `env` table) |
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
│ _enrich_project = enrich L1+L2 (local qwen3, think=F)    │
│   → classify semantic_type (cloud DeepSeek)              │
│   → build_wiki   (embeddings GPU-only, CPU fatal)        │
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
| **HR6** | GPU-only; any CPU fallback raises fatally. (≡ inv#5.) |
| **HR7** | `kb_state` lifecycle: `indexing → searchable → enriching → ready`; `ready` iff `enriched_pct ≥ 95 AND l2_enriched_pct == 100`. Federated entity = worst-of members. |
| **HR8** | Two-stage retrieval at query time: Stage 1 = bi-encoder vector search (`sqlite-vec`); Stage 2 = cross-encoder rerank (`jina-reranker-v1-turbo-en`, GPU). Both AXIS A (code chunks) and AXIS B (community summaries) are reranked. Results ordered by `rerank_score`, never the bare vector `score`. Reranking never runs at index/KB-build time. |
| **HR9** | MCP query actions (`search`/`ask`/`graph`/`overview`) perform ONLY embedding + reranking — no generative LLM (cloud or local). `ask` → `compose_answer` = assembled context; generation is delegated to the calling agent (June 2026 MCP best practice). |
| **HR10** | Dashboard chat (`POST /api/chat_stream`) uses **claude-haiku-4-5** via the Claude Code CLI (`claude -p --model`). **Codex support removed** — no codex, no ollama, no deepseek in the chat path. Dashboard-only: not wired to any MCP tool. |
| **HR11** | `semantic_type` is assigned by **direct LLM classification (cloud DeepSeek `deepseek-chat`)** over title+summary (batch-20), local ollama as fallback. The daemon classifies only new/unclassified communities (`reclassify_all=False`) → never re-labels settled ones (no churn). A `<3`-member community cannot be `business_process`/`business_rule` (structural guard → `feature`). No embeddings/prototypes in classification. |
| **HR12** | The build-path **local LLM is never idle-resident-spinning**: every `graph/llm.py:chat()` call sends `think=False` + bounded `num_predict` so qwen3 cannot emit unbounded `<think>` output that truncates at the context limit and busy-spins (#13461). Idle daemon keeps no LLM core pinned; the model unloads at keep_alive idle. (GPU-max / CPU-RAM-min.) |

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
9. **MCP query actions never generate** — the cloud/query generative LLM (codex→haiku) is
   reached only via the dashboard chat box (`POST /api/chat_stream`); MCP query actions
   (`search`/`ask`/`graph`/`overview`) and `compose_answer` never generate text; the local
   generative LLM (ollama) is confined to background KB enrichment. (≡ HR9.)
10. **Reranking is the relevance authority** — every query result set is ordered by
    `rerank_score`, never the bare vector `score`; cross-encoder runs at query time only. (≡ HR8.)
11. **Both retrieval axes are cross-encoder-ranked** — AXIS A (code chunks) and AXIS B
    (community/architecture context) both pass through `jina-reranker-v1-turbo-en` at
    query time before the context is assembled. (≡ HR8.)
12. **Dashboard chat uses claude-haiku-4-5** via the Claude Code CLI; **codex removed**; never
    ollama/deepseek. (≡ HR10.)

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
| HR10 dashboard chat codex→haiku | `test_e6_dashboard_chat_codex_haiku_only`, `test_chat_stream_sse_sends_done` | `test_p5_server.py` / `test_p4_query.py` |
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
